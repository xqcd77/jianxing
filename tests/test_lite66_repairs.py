"""User-visible multi-turn regressions; bank receipts remain the authority."""
import json

import httpx
import pytest
from sqlalchemy import func, select

from bank_agent.errors import BankError
from bank_agent.goal_state import parse_goal_patch
from bank_agent.research import ResearchService
from bank_agent.schemas import AdditionalGoal, Intent
from bank_agent.storage import Plan, Receipt
from bank_agent.supervisor_agents import BusinessModel
from conftest import finish, login, payroll, start
from test_public_information import ask, source
from test_request_goal_repairs import ETF_DIRECTORY
from test_research import research


def bank_goals(result):
    return [g for g in result['request_goals'] if g['kind'] == 'bank_operation']


def test_aa_collection_receipt_is_not_a_paid_receipt(client, headers):
    payroll(client, headers)
    created = finish(client, headers, start(client, 'aa.create', {
        'amount_fen': 10000, 'participants': ['u_demo', 'u_wang', 'u_li'], 'subject': '聚餐AA'}))
    shares = created['nodes'][0]['result']['data']['shares']
    request_id = created['nodes'][0]['result']['data']['request_id']
    assert [(s['amount_fen'], s['status']) for s in shares] == [(3334, 'SELF_SHARE'), (3333, 'UNPAID'), (3333, 'UNPAID')]
    assert '还有2人合计66.66元未支付，尚未收齐' in created['assistant_message']
    assert '发起人自担' in created['assistant_message']
    assert '本次办理已完成' not in created['assistant_message']
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 600000

    wang = login(client, 'wang')
    finish(client, wang, start(client, 'aa.pay', {'request_id': request_id}, user_id='u_wang'), user_id='u_wang')
    partial = start(client, 'aa.status', {'request_id': request_id})
    assert '已收33.33元，还有1人合计33.33元未支付' in partial['assistant_message']
    li = login(client, 'li')
    finish(client, li, start(client, 'aa.pay', {'request_id': request_id}, user_id='u_li'), user_id='u_li')
    settled = start(client, 'aa.status', {'request_id': request_id})
    assert '应付份额已收齐，共66.66元' in settled['assistant_message']
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 606666
    replay = client.post('/api/plans/' + created['id'] + '/resume', headers=headers, json={})
    assert replay.status_code == 200
    assert '发起时已收0元' in replay.json()['assistant_message']
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 606666


@pytest.mark.parametrize('message', [
    '不是取消转账，是把给小王的那笔改给小李，金额不变；妈妈那笔和生活费预留保持，等我确认再办。',
    '妈妈那笔不变；把给小王的900元改给小李，生活费保持。',
])
def test_change_one_named_recipient_preserves_all_siblings(client, headers, message):
    payroll(client, headers)
    initial = ask(client, headers, '给妈妈转400元，给小王转900元，预留生活费1000元，先列计划等我确认。')
    changed = ask(client, headers, message, initial['conversation_id'])
    assert changed['id'] == initial['id'] and changed['version'] > initial['version']
    transfers = [n['params'] for n in changed['nodes'] if n['operation'] == 'funds.transfer']
    assert [(p['recipient_hint'], p['amount_fen']) for p in transfers] == [('妈妈', 40000), ('小李', 90000)]
    assert next(n for n in changed['nodes'] if n['operation'] == 'funds.reserve')['params']['amount_fen'] == 100000
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 600000


def test_remaining_ratio_budget_and_visible_result_refresh_after_transfer_edit(client, headers):
    payroll(client, headers)
    source(client, funds=ETF_DIRECTORY)
    initial = ask(client, headers, '给妈妈转400元，给小王转1500元，剩下的钱的20%用来看看ETF产品，只查资料，先等我确认。')
    changed = ask(client, headers, '只把给小王的金额改为800元，妈妈不变，ETF仍按转账后剩余金额的20%。', initial['conversation_id'])
    assert changed['id'] == initial['id']
    goal = next(g for g in changed['request_goals'] if g['kind'] == 'public_information')
    assert goal['query']['budget_fen'] == 96000
    assert goal['result']['investment_budget']['budget_fen'] == 96000
    assert '960' in goal['result']['message'] and '960' in goal['notice']
    assert '820' not in goal['result']['message']
    refreshed = client.get('/api/plans/' + changed['id'], headers=headers).json()
    assert next(g for g in refreshed['request_goals'] if g['kind'] == 'public_information')['query']['budget_fen'] == 96000


@pytest.mark.parametrize('modification', ['减少200元', '少转200元'])
def test_incidental_answer_never_replaces_pending_plan_anchor(client, headers, modification):
    payroll(client, headers)
    initial = ask(client, headers, '给妈妈转1200元，给小王转300元，请先核对并等我确认。')
    cid = initial['conversation_id']
    # This is the actual typed incidental answer returned in the failed live
    # trace, without involving a remote model or matching exam-specific words.
    client.app.state.graph.start('u_demo', '顺便说，我在准备考试，共4500道题，满分100分。', cid,
        intent=Intent(summary='非银行信息', additional_goals=[AdditionalGoal(kind='clarification',
            title='考试信息无需办理', source_text='4500道题、100分', message='本次不涉及银行办理。')]))
    changed = ask(client, headers, '回到前面的转账：给妈妈那笔' + modification + '，小王的保持。刚才4500道题不是钱。', cid)
    assert changed['id'] == initial['id']
    assert [(n['params']['recipient_hint'], n['params']['amount_fen']) for n in changed['nodes']] == [('妈妈', 100000), ('小王', 30000)]
    with client.app.state.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Plan)) == 1
        assert s.scalar(select(func.count()).select_from(Receipt).where(Receipt.operation == 'funds.transfer')) == 0


def test_completed_transfer_is_immutable_but_bill_can_be_queried_again(client, headers):
    payroll(client, headers)
    original = ask(client, headers, '给妈妈转500元，再查本月账单。')
    completed = finish(client, headers, original)
    assert completed['status'] == 'COMPLETED'
    changed = ask(client, headers, '刚才已完成的转账保持，不用再转。只把账单月份换成2026-09重新查询，原转账回执保留。', original['conversation_id'])
    assert [n['operation'] for n in changed['nodes']] == ['bills.report']
    assert changed['nodes'][0]['params']['period'] == '2026-09'
    assert changed['nodes'][0]['status'] == 'SUCCEEDED'
    assert changed['completed_history'][0]['plan_id'] == original['id']
    assert '500' in changed['assistant_message'] and '不重复转账' in changed['assistant_message']
    old = client.get('/api/plans/' + original['id'], headers=headers).json()
    assert old['nodes'][0]['status'] == 'SUCCEEDED'
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 550000
    with client.app.state.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Receipt).where(Receipt.operation == 'funds.transfer')) == 1


def test_completed_money_goal_cannot_be_patched_to_reexecute(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, ask(client, headers, '给妈妈转500元'))
    result = client.post('/api/conversations/' + plan['conversation_id'] + '/goals', headers=headers,
        json={'target_id': bank_goals(plan)[0]['id'], 'expected_revision': plan['goal_revision'],
              'fields': {'amount_fen': 100000}})
    assert result.status_code == 409 and result.json()['error']['code'] == 'GOAL_NOT_EDITABLE'


def test_unknown_target_is_not_guessed_from_an_unrelated_amount():
    data = {'revision': 1, 'goals': [{'id': 'a', 'kind': 'bank_operation', 'operation': 'bills.report',
        'title': '账单', 'status': 'SUCCEEDED', 'params': {'period': '2026-10'}}]}
    patch = parse_goal_patch('月份换成2026-09', data, {'business_now': '2026-10-13'})
    assert patch.fields == {'period': '2026-09', 'annual': False}


def test_existing_reserve_suffix_does_not_become_a_new_reserve():
    slots = ResearchService._anchored_amounts('生活费还要留1000，应急2000已经预留别动。')
    assert slots['reserve_fen'] == 100000 and slots.get('emergency_fen', 0) == 0
    assert ResearchService._existing_reserve_fields('生活费还要留1000，应急2000已经预留别动。') == {'emergency_fen'}


def test_invalid_tool_fields_are_repairable_before_service_dispatch():
    payload = {'summary': '月报', 'tasks': [{'domain': 'bills', 'action': 'bills.report',
        'params': {'period': '2026-09', 'made_up_filter': True}}]}
    response = httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload)}}]})
    with pytest.raises(BankError) as caught:
        BusinessModel._decode(response, Intent)
    assert caught.value.code == 'MODEL_RESPONSE_INVALID'
    assert caught.value.diagnostic['kind'] == 'schema_validation'


def test_research_uses_saved_current_preferences_not_only_query_text(client, headers):
    from bank_agent.storage import User
    with client.app.state.db.write() as s:
        s.get(User, 'u_demo').risk_level = 3
    payroll(client, headers)
    saved = client.post('/api/memory/preferences', headers=headers, json={
        'key': 'investment.shortlist', 'expected_version': 0, 'domains': ['wealth'],
        'text': '只把模拟现金产品作为候选；不推荐模拟稳健产品和模拟30天产品，不要自动申购。'})
    assert saved.status_code == 200
    result = research(client, headers, query='按照我保存的长期产品偏好比较模拟产品，只做分析，不办理。')
    assert result['status'] == 'RESEARCH_READY', result
    products = {p['product_id']: p for p in result['report']['products']}
    assert not products['P_CASH']['excluded']
    assert products['P_STEADY']['excluded'] and products['P_LOCKED']['excluded']
    assert result['report']['financial_operations_executed'] is False


def test_research_budget_does_not_double_count_existing_hold_or_pending_transfer(tmp_path):
    from evaluation.servicebench.lite66 import scenarios
    from evaluation.servicebench.memory_context_runtime import MemoryContextSpecialistEpisode
    case = next(s for s in scenarios() if s.execution.id == 'special_dr08')
    episode = MemoryContextSpecialistEpisode(case, 'SUPERVISOR', tmp_path, 'demo', 'lexical')
    result = episode.run()
    assert result['sut_error'] is None
    assert result['rules']['safe_success']
    report = next(t['response']['report'] for t in result['responses'] if t['response'].get('report'))
    assert report['funding']['investable_ceiling_fen'] == 330000
    assert '9000' in report['summary'] and '3300' in report['summary']


def test_public_product_details_followup_keeps_same_product_and_read_only_scope():
    from bank_agent.information import information_intent
    from datetime import datetime, timezone
    context = {'last_information_query': {'topic': 'fund_products', 'organization': '嘉实', 'product_hint': '000082'},
        'last_information_query_at': datetime.now(timezone.utc).isoformat()}
    intent = information_intent('重点看赎回到账、费用和股票风险，不要申购。', context)
    assert intent.information_query.product_hint == '000082'
    assert intent.information_query.organization == '嘉实' and not intent.tasks


def test_dated_bill_question_has_no_invented_tool_filters(client, headers):
    payroll(client, headers)
    plan = ask(client, headers, '2026年9月我一共消费了多少？转账单独列，不算消费。')
    assert len(plan['nodes']) == 1 and plan['nodes'][0]['operation'] == 'bills.report'
    assert plan['nodes'][0]['params'] == {'period': '2026-09', 'annual': False}
    assert plan['nodes'][0]['status'] == 'SUCCEEDED'
    assert '2026-09' in plan['assistant_message']


def test_dated_bill_shortcut_does_not_discard_a_new_unrelated_goal():
    from bank_agent.source_intents import _complete_bill_question
    assert not _complete_bill_question('2026年9月消费是多少，再帮我解锁卡片')
    assert not _complete_bill_question('2026年9月消费是多少，顺便解释这个体检报告')


def test_tool_parameter_validation_preserves_typed_dag_references():
    payload = {'summary': '新卡限制', 'tasks': [{'domain': 'cards', 'action': 'cards.restrict',
        'params': {'card_id': '$new_card.card_id', 'single_limit_fen': 60000}}]}
    response = httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload)}}]})
    result = BusinessModel._decode(response, Intent)
    assert result.tasks[0].params['card_id'] == '$new_card.card_id'


def test_fixed_budget_after_remaining_word_is_not_replaced_by_remaining_balance():
    from bank_agent.advisory_budget import bind_budget_rule
    assert bind_budget_rule({'query': {'currency': 'CNY'}, 'source_text': '剩下的钱1000块投资ETF'}) is None
    rule = bind_budget_rule({'query': {'currency': 'CNY'}, 'source_text': '剩下的钱的20%看ETF'})
    assert rule['fraction'] == '0.2'


@pytest.mark.parametrize('ratio,expected', [('20%', 82000), ('百分之二十', 82000), ('五分之一', 82000), ('一半', 205000)])
def test_pre_review_and_post_edit_advisory_calculation_agree(ratio, expected):
    from bank_agent.advisory_budget import bind_budget_rule, derive_budget
    from bank_agent.information import ground_investment_query
    from bank_agent.schemas import PublicInformationQuery
    message = '给妈妈转400元，给小王转1500元，剩余资金的' + ratio + '用于ETF资料比较。'
    query = PublicInformationQuery(topic='fund_products', asset_type='etf', budget_fen=82000)
    grounded = ground_investment_query(query, message, 190000, 600000)
    assert grounded.budget_fen == expected
    rule = bind_budget_rule({'source_text': message, 'query': query.model_dump(mode='json')})
    nodes = [{'operation': 'funds.transfer', 'status': 'WAIT_CONFIRM', 'params': {'amount_fen': 190000}}]
    assert derive_budget(rule, {'account': {'available_fen': 600000}}, nodes)[0] == expected


def test_model_proposal_budget_is_not_overwritten_with_the_entire_remainder(client, headers):
    from bank_agent.schemas import PublicInformationQuery
    payroll(client, headers)
    intent = Intent(summary='转账并查ETF资料', tasks=[
        {'domain': 'funds', 'action': 'funds.transfer', 'params': {'recipient_hint': '妈妈', 'amount_fen': 40000}},
        {'domain': 'funds', 'action': 'funds.transfer', 'params': {'recipient_hint': '小王', 'amount_fen': 150000}},
    ], additional_goals=[AdditionalGoal(kind='public_information', title='ETF资料', source_text='剩余资金20%查询ETF',
        information_query=PublicInformationQuery(topic='fund_products', asset_type='etf', budget_fen=82000))])
    reconciled = client.app.state.graph.planner._reconcile('u_demo', None, None, intent,
        '给妈妈转400元，给小王转1500元，剩余资金的20%用于ETF资料比较。仅查询资料，不申购。')
    assert [t.params['amount_fen'] for t in reconciled.tasks if t.action == 'funds.transfer'] == [40000, 150000]
    assert reconciled.additional_goals[0].information_query.budget_fen == 82000


def test_fixed_amount_after_remaining_is_grounded_before_review():
    from bank_agent.information import ground_investment_query
    from bank_agent.schemas import PublicInformationQuery
    query = PublicInformationQuery(topic='fund_products', asset_type='etf', budget_fen=550000)
    result = ground_investment_query(query, '给妈妈转500元，剩下的钱拿1000元用于ETF资料比较。', 50000, 600000)
    assert result.budget_fen == 100000


def test_ordinary_reserve_does_not_require_an_invented_optional_goal(client, headers):
    payroll(client, headers)
    planner = client.app.state.graph.planner
    message = '给妈妈转400元，给小王转900元，再预留生活费1000元。先列草案，不要执行。'
    intent = Intent(summary='转账及生活费预留', tasks=[
        {'domain': 'funds', 'action': 'funds.transfer', 'params': {'recipient_hint': '妈妈', 'amount_fen': 40000}},
        {'domain': 'funds', 'action': 'funds.transfer', 'params': {'recipient_hint': '小王', 'amount_fen': 90000}},
        {'domain': 'funds', 'action': 'funds.reserve', 'params': {'amount_fen': 100000, 'purpose': '生活费', 'goal_id': 'invented_living_reserve'}}])
    grounded = planner._reconcile('u_demo', None, None, intent, message)
    assert grounded.tasks[-1].params['goal_id'] == ''
    draft = client.app.state.graph.start('u_demo', message, intent=grounded)
    assert len(draft['nodes']) == 3
    assert all(n['status'] in {'WAIT_CONFIRM', 'WAIT_MFA', 'READY'} for n in draft['nodes'])
    assert any(n['status'] in {'WAIT_CONFIRM', 'WAIT_MFA'} for n in draft['nodes'])
    assert client.get('/api/overview', headers=headers).json()['account']['reserved_fen'] == 0
    assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 600000


@pytest.mark.parametrize('goal_id,message', [
    ('missing_user_goal', '为目标missing_user_goal预留1000元'),
    ('$created_goal.goal_id', '创建生日目标并预留1000元'),
])
def test_reserve_keeps_explicit_goal_ids_and_dependency_references(client, goal_id, message):
    intent = Intent(summary='目标预留', tasks=[{'domain': 'funds', 'action': 'funds.reserve',
        'params': {'amount_fen': 100000, 'purpose': '目标预算', 'goal_id': goal_id}}])
    result = client.app.state.graph.planner._reconcile('u_demo', None, None, intent, message)
    assert result.tasks[0].params['goal_id'] == goal_id


def test_auto_purchase_prohibition_does_not_exclude_the_product_itself():
    from bank_agent.research_preferences import product_exclusions
    products = [{'id': 'a', 'name': '甲产品'}, {'id': 'b', 'name': '乙产品'}]
    assert product_exclusions([{'text': '不要自动申购甲产品'}], products, '') == []
    assert product_exclusions([{'text': '不是只把甲产品作为候选'}], products, '') == []
