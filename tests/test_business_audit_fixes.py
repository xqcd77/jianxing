from datetime import datetime
from sqlalchemy import select
from bank_agent.storage import Document, Receipt, save_document, User
from conftest import approve, finish, payroll, start


def test_duplicate_alert_contains_traceable_business_details(client, headers):
    plan = client.post('/api/businesses/bills/anomalies/prepare', headers=headers, json={'params':{}}).json()
    done = finish(client, headers, plan)
    result = next(n['result']['data'] for n in done['nodes'] if n['operation']=='bills.anomalies')
    assert result['classification']=='suspected_not_confirmed_fraud'
    assert result['alerts']
    for alert in result['alerts']:
        assert len(alert['transactions']) >= 2
        assert {t['transaction_id'] for t in alert['transactions']} == set(alert['transaction_ids'])
        assert all(t['date'] and t['description'] and t['amount_fen']>0 for t in alert['transactions'])


def test_named_contract_requires_query_and_preserves_bank_only_scope(client, headers):
    plan = client.post('/api/chat', headers=headers, json={'message': '只撤销视频会员的银行代扣，保留商户合同'}).json()
    assert 'nodes' in plan, plan
    assert plan['nodes'][0]['operation'] == 'subscriptions.query'
    assert plan['nodes'][0]['status'] == 'WAIT_CONFIRM'
    done = finish(client, headers, plan)
    assert done['status'] == 'COMPLETED'
    with client.app.state.db.read() as s:
        video = s.get(Document, 'S_VIDEO').data
        assert video['bank_mandate'] == 'REVOKED' and video['merchant_contract'] == 'ACTIVE'


def test_bank_mandate_request_does_not_require_the_word_only(client, headers):
    plan = client.post('/api/chat', headers=headers, json={'message': '取消视频会员的银行代扣'}).json()
    assert finish(client, headers, plan)['status'] == 'COMPLETED'
    with client.app.state.db.read() as s:
        assert s.get(Document, 'S_VIDEO').data['merchant_contract'] == 'ACTIVE'


def test_duplicate_contract_name_is_not_arbitrarily_selected(client, headers):
    with client.app.state.db.write() as s:
        original = s.get(Document, 'S_VIDEO').data
        save_document(s, 'S_VIDEO_DUP', 'subscription', 'u_demo', {**original, 'id': 'S_VIDEO_DUP'})
    plan = client.post('/api/chat', headers=headers, json={'message': '取消视频会员的银行代扣'}).json()
    done = finish(client, headers, plan)
    assert done['status'] != 'COMPLETED'
    with client.app.state.db.read() as s:
        assert s.get(Document, 'S_VIDEO').data['bank_mandate'] == 'ACTIVE'
        assert s.get(Document, 'S_VIDEO_DUP').data['bank_mandate'] == 'ACTIVE'


def test_contract_change_invalidates_bound_confirmation(client, headers):
    plan = client.post('/api/chat', headers=headers, json={'message': '只撤销视频会员的银行代扣'}).json()
    plan = approve(client, headers, plan).json()
    with client.app.state.db.write() as s:
        contract = s.get(Document, 'S_VIDEO')
        contract.data = {**contract.data, 'amount_fen': 9900}
    node = next(n for n in plan['nodes'] if n['operation'] == 'subscriptions.cancel')
    response = approve(client, headers, plan, node)
    assert response.status_code in {200, 409}
    with client.app.state.db.read() as s:
        assert s.get(Document, 'S_VIDEO').data['bank_mandate'] == 'ACTIVE'


def test_second_purchase_does_not_relock_mature_shares(client, headers):
    payroll(client, headers, 1000000)
    with client.app.state.db.write() as s:
        s.get(User, 'u_demo').risk_level = 3
        for key in ('S_VIDEO', 'S_MUSIC'):
            doc = s.get(Document, key)
            doc.data = {**doc.data, 'bank_mandate': 'REVOKED'}
    first = finish(client, headers, start(client, 'wealth.purchase', {'product_id': 'P_LOCKED', 'amount_fen': 50000}))
    assert first['status'] == 'COMPLETED'
    client.post('/api/demo/clock', headers=headers, json={'days': 31})
    finish(client, headers, start(client, 'wealth.purchase', {'product_id': 'P_LOCKED', 'amount_fen': 50000}))
    redeemed = finish(client, headers, start(client, 'wealth.redeem', {'product_id': 'P_LOCKED', 'amount_fen': 50000}))
    assert redeemed['status'] == 'COMPLETED'
    with client.app.state.db.read() as s:
        holding = s.get(Document, 'holding_u_demo_P_LOCKED').data
        assert holding['principal_fen'] == 50000 and len(holding['lots']) == 2
        assert holding['lots'][0]['units_milli'] == 0 and holding['lots'][1]['units_milli'] == 500000


def test_automatic_debit_has_one_receipt_and_one_posting(client, headers):
    payroll(client, headers)
    client.post('/api/demo/clock', headers=headers, json={'days': 7})
    client.post('/api/scheduler/tick', headers=headers)
    with client.app.state.db.read() as s:
        receipts = list(s.scalars(select(Receipt).where(Receipt.operation == 'event.subscription')))
        assert len(receipts) == 2
        assert {r.result['data']['amount_fen'] for r in receipts} == {1900, 2900}
        assert all(s.get(Document, r.id).data['receipt_id'] == r.id for r in receipts)


def test_settlement_uses_configured_closures_and_weekend(client):
    from bank_agent.banking_calendar import settlement_date
    with client.app.state.db.write() as s:
        save_document(s, 'banking_calendar', 'banking_calendar', 'system', {'closed_dates': ['2026-10-19']})
        assert settlement_date(s, datetime.fromisoformat('2026-10-16T16:00:00+08:00'), 1).isoformat() == '2026-10-21'
        assert settlement_date(s, datetime.fromisoformat('2026-10-17T09:00:00+08:00'), 1).isoformat() == '2026-10-21'


def test_legacy_event_receipt_migration_never_reposts_money(client, headers):
    from bank_agent.migrations import migrate_business_evidence
    from bank_agent.storage import Account, LedgerEntry
    payroll(client, headers)
    client.post('/api/demo/clock', headers=headers, json={'days': 7})
    with client.app.state.db.write() as s:
        balance = s.get(Account, 'a_demo').balance_fen
        count = len(list(s.scalars(select(LedgerEntry))))
        s.delete(s.get(Document, 'migration_business_evidence_v3'))
        for row in s.scalars(select(Receipt).where(Receipt.operation == 'event.subscription')):
            s.delete(row)
    migrate_business_evidence(client.app.state.db)
    with client.app.state.db.read() as s:
        assert s.get(Account, 'a_demo').balance_fen == balance
        assert len(list(s.scalars(select(LedgerEntry)))) == count
        receipts = list(s.scalars(select(Receipt).where(Receipt.operation == 'event.subscription')))
        assert len(receipts) == 2 and all(r.result['data']['historical_balance_unknown'] for r in receipts)
