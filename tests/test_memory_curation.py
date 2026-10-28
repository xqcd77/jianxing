"""Background memory policy, recovery and source-preserving curation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.memory import SharedMemory
from bank_agent.memory_policy import capture_decision, contains_credentials
from bank_agent.memory_rules import state
from bank_agent.storage import Document, MemoryRecord, canonical, now_iso
from conftest import login, payroll, start
from test_research import counts, research


def drain(memory):
    for _ in range(30):
        result = memory.curator.run_pending()
        if not result['more']:
            return
    raise AssertionError('curation failed to finish bounded work')


def record(memory, text, *, owner='u_demo', chat='curation', **kwargs):
    return memory.remember(owner, text, roles=['funds'], conversation_id=chat, **kwargs)


def stored(memory, key):
    with memory.db.read() as session:
        return session.get(MemoryRecord, key)


def test_job_is_durable_coalesced_and_only_contains_metadata(client):
    memory = client.app.state.memory
    one = record(memory, '给妈妈转400元，不要买理财')
    record(memory, '生活费留600元')
    value = memory.curator.status('u_demo')
    assert value['status'] == 'pending' and value['generation'] == 2
    with memory.db.read() as session:
        jobs = list(session.scalars(select(Document).where(Document.kind == 'memory_maintenance')))
    assert len(jobs) == 1 and '妈妈' not in canonical(jobs[0].data)
    drain(memory)
    assert memory.curator.status('u_demo')['processed_generation'] == 2
    assert stored(memory, one).text == '给妈妈转400元，不要买理财'
    metadata = state(stored(memory, one))['curation']
    assert len(metadata['source_digest']) == 64 and metadata['authority'].startswith('derived_context_only')


def test_exact_duplicates_keep_originals_but_do_not_crowd_retrieval(client):
    memory = client.app.state.memory
    ids = [record(memory, '给妈妈转400元，不要申购') for _ in range(3)]
    drain(memory)
    originals = [stored(memory, key) for key in ids]
    assert len([r for r in originals if state(r)['status'] == 'active']) == 1
    assert len([r for r in originals if state(r)['status'] == 'duplicate']) == 2
    hits = memory.search('u_demo', '妈妈', 'funds', 'curation')['items']
    assert len(hits) == 1
    assert all(memory.reread('u_demo', key, conversation_id='curation')['text'] == '给妈妈转400元，不要申购' for key in ids)
    canonical_id = hits[0]['id']
    memory.forget('u_demo', canonical_id)
    memory.curator.bootstrap()
    drain(memory)
    assert memory.search('u_demo', '妈妈', 'funds', 'curation')['items'] == []


def test_no_semantic_merging_across_values_negation_roles_users_or_chats(client):
    memory = client.app.state.memory
    ids = [record(memory, text, chat=chat, owner=owner) for text, chat, owner in [
        ('给妈妈转400元', 'one', 'u_demo'), ('给妈妈转500元', 'one', 'u_demo'),
        ('不要给妈妈转400元', 'one', 'u_demo'), ('给妈妈转400元', 'two', 'u_demo'),
        ('给妈妈转400元', 'one', 'u_wang')]]
    ids.append(memory.remember('u_demo', '给妈妈转400元', roles=['wealth'], conversation_id='one'))
    drain(memory)
    assert all(state(stored(memory, key))['status'] == 'active' for key in ids)


def test_curation_never_uses_similarity_to_overwrite_keyed_preferences(client):
    memory = client.app.state.memory
    ids = [memory.remember('u_demo', '不要动用500元', kind='preference', scope='user', roles=['funds'],
        source_type='user_preference', conflict_key=key, expected_version=0) for key in ('rent', 'birthday')]
    drain(memory)
    assert all(state(stored(memory, key))['status'] == 'active' for key in ids)
    same = memory.remember('u_demo', '不要动用500元', kind='preference', scope='user', roles=['funds'],
        source_type='user_preference', conflict_key='rent', expected_version=1)
    assert same == ids[0]


def test_archives_only_old_noncritical_dialogue_and_keeps_raw_source(client):
    memory = client.app.state.memory
    memory.curator.active_dialogues = 1
    first = record(memory, '我在了解这个页面的功能')
    record(memory, '先看看有哪些服务')
    drain(memory)
    assert state(stored(memory, first))['status'] == 'archived'
    assert memory.reread('u_demo', first, conversation_id='curation')['text'] == '我在了解这个页面的功能'
    assert first not in {v['id'] for v in memory.search('u_demo', '页面', 'funds', 'curation')['items']}


def test_active_tasks_and_financial_negations_are_never_archived(client, headers):
    payroll(client, headers)
    plan = start(client, 'funds.transfer', {'recipient_hint': '妈妈', 'amount_fen': 40000})
    memory = client.app.state.memory
    memory.curator.active_dialogues = 1
    ongoing = record(memory, '本次办理还没有结束', chat=plan['conversation_id'])
    negative = record(memory, '不要动用生日预算500元', chat='old_closed')
    structured = record(memory, '给家人留一笔钱', chat='old_closed', payload={'amount_fen': 60000})
    record(memory, '最近正在查看其他功能', chat='newer')
    drain(memory)
    assert all(state(stored(memory, key))['status'] == 'active' for key in [ongoing, negative, structured])


def test_new_task_after_batch_snapshot_prevents_archival(client):
    memory = client.app.state.memory
    key = record(memory, '办理尚待继续的已填信息', chat='newly_opened')
    with memory.db.write() as session:
        session.add(Document(id='draft_u_demo_newly_opened', owner_id='u_demo', kind='draft',
                             data={'intent': {'clarification': '补充收款人'}}))
    # Batch-start snapshot had no open task; commit must check live SQL again.
    assert memory.curator._organize('u_demo', key, set(), set()) == 'active'
    assert state(stored(memory, key)).get('status', 'active') == 'active'


def test_expired_and_superseded_rows_are_deindexed_without_erasing_sources(client):
    memory = client.app.state.memory
    first = memory.remember('u_demo', '详细账单', kind='preference', scope='user', roles=['bills'],
        source_type='user_preference', conflict_key='style', expected_version=0)
    memory.search('u_demo', '账单', 'bills')
    second = memory.remember('u_demo', '简洁账单', kind='preference', scope='user', roles=['bills'],
        source_type='user_preference', conflict_key='style', expected_version=1)
    expired = record(memory, '即将过期的历史记录')
    with memory.db.write() as session:
        session.get(MemoryRecord, expired).expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    drain(memory)
    assert state(stored(memory, first))['status'] == 'superseded'
    assert state(stored(memory, expired))['status'] == 'expired'
    assert memory.reread('u_demo', first)['superseded_by'] == second
    assert state(stored(memory, second))['status'] == 'active'
    client_index = memory.index._open()
    for collection in client_index.list_collections():
        index = client_index.get_collection(collection.name, embedding_function=None)
        assert not index.get(ids=[first, expired])['ids']


def test_index_failure_leaves_retryable_job_and_sql_blocks_stale_hit(client, monkeypatch):
    memory = client.app.state.memory
    ids = [record(memory, '相同的历史上下文') for _ in range(2)]
    original = memory.index.delete

    def failed(_):
        raise BankError('MEMORY_INDEX_UNAVAILABLE', 'synthetic failure', 503)

    monkeypatch.setattr(memory.index, 'delete', failed)
    drain(memory)
    assert memory.curator.status('u_demo')['status'] == 'retry'
    assert len([key for key in ids if state(stored(memory, key))['status'] == 'duplicate']) == 1
    monkeypatch.setattr(memory.index, 'delete', original)
    memory.curator.bootstrap()
    drain(memory)
    assert memory.curator.status('u_demo')['status'] == 'idle'


def test_running_lease_recovers_after_restart_without_broadening_permissions(tmp_path):
    settings = Settings(data_dir=tmp_path, memory_backend='numpy', memory_maintenance_batch=1)
    with TestClient(create_app(settings)) as client:
        record(client.app.state.memory, '旧会话预算400元')
        record(client.app.state.memory, '另一笔预算500元', owner='u_wang')
        with client.app.state.db.write() as session:
            job = session.scalar(select(Document).where(Document.kind == 'memory_maintenance', Document.owner_id == 'u_demo'))
            job.data = {**job.data, 'status': 'running', 'lease_until': '2000-01-01T00:00:00+00:00'}
    with TestClient(create_app(settings)) as client:
        memory = client.app.state.memory
        drain(memory)
        assert memory.curator.status('u_demo')['status'] == 'idle'
        result = memory.search('u_demo', '预算', 'funds', 'curation')
        assert '另一笔' not in canonical(result)


def test_mid_batch_new_write_is_not_lost(client, monkeypatch):
    memory = client.app.state.memory
    record(memory, '最初的一笔400元')
    organize, inserted = memory.curator._organize, []

    def concurrent(*args):
        if not inserted:
            inserted.append(record(memory, '处理期间新增500元'))
        return organize(*args)

    monkeypatch.setattr(memory.curator, '_organize', concurrent)
    drain(memory)
    assert state(stored(memory, inserted[0]))['curation']
    assert memory.curator.status('u_demo')['generation'] == memory.curator.status('u_demo')['processed_generation']


def test_workers_cannot_claim_same_background_batch(client):
    memory = client.app.state.memory
    record(memory, '需要整理的400元')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: memory.curator.run_pending(), range(2)))
    assert sum(r['jobs'] for r in results) == 1


@pytest.mark.parametrize('payload', [{'params': {'otp': '123456'}}, {'api_key': 'credential'},
                                   {'nested': [{'refresh_token': 'credential'}]}])
def test_sensitive_structured_payload_cannot_enter_memory(client, payload):
    with pytest.raises(BankError) as caught:
        record(client.app.state.memory, '普通文字', payload=payload)
    assert caught.value.code == 'MEMORY_CONTENT_REJECTED'
    assert contains_credentials(canonical(payload))


@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), 3651])
def test_invalid_ttl_is_rejected_before_writing(client, value):
    with pytest.raises(BankError) as caught:
        record(client.app.state.memory, '历史记录', ttl_days=value)
    assert caught.value.code == 'MEMORY_TTL_INVALID'


@pytest.mark.parametrize('message', ['好的', '继续', '好了吗', '停止', '取消', '谢谢'])
def test_task_control_not_promoted_to_memory_but_keeps_real_receipts(client, monkeypatch, message):
    memory, receipts = client.app.state.memory, []
    monkeypatch.setattr(memory, 'capture_results', lambda *args: receipts.append(args))
    response = {'status': 'WAIT_CONFIRM'}
    assert capture_decision(message, response, {'tasks': [{'domain': 'funds'}]}) == 'skip_task_control'
    memory.capture('u_demo', 'control_chat', message, response, {'tasks': [{'domain': 'funds'}]})
    assert receipts and not memory.list('u_demo')


def test_balance_read_has_no_vector_dependency_but_required_preferences_remain(client, monkeypatch):
    memory = client.app.state.memory
    key = memory.remember('u_demo', '不要动用生日预算500元', kind='preference', scope='user', roles=['funds'])

    def forbidden(*args, **kwargs):
        raise AssertionError('current balance must not use semantic retrieval')

    monkeypatch.setattr(memory.index, 'rank', forbidden)
    monkeypatch.setattr(memory.embeddings, 'encode', forbidden)
    result = memory.search_for_request('u_demo', '查余额', 'funds')
    assert result['read_policy'] == 'current_facts_and_preferences'
    assert key in {v['id'] for v in result['items']}
    assert result['protected']['negative_constraints'] == ['不要动用生日预算500元']


@pytest.mark.parametrize('query', ['继续', '上次转了多少', '把剩下的一半给妈妈', '查余额并转400元', '给妈妈转400元'])
def test_contextual_composed_or_financial_requests_keep_recall(client, monkeypatch, query):
    memory, calls = client.app.state.memory, []
    original = memory.search

    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, 'search', capture)
    memory.search_for_request('u_demo', query, 'funds', 'one')
    assert calls and not calls[0].get('_preferences_only')


def test_curation_status_endpoint_is_owner_scoped_and_authenticated(client, headers):
    memory = client.app.state.memory
    record(memory, '本人的业务400元')
    assert client.get('/api/memory/maintenance').status_code == 401
    mine = client.get('/api/memory/maintenance', headers=headers).json()
    other = client.get('/api/memory/maintenance', headers=login(client, 'li')).json()
    assert mine['generation'] == 1 and 'generation' not in other


def test_research_curation_preserves_evidence_and_makes_no_financial_writes(client, headers):
    payroll(client, headers)
    result = research(client, headers, {'analysis_only': True, 'forbid_purchase': True})
    assert result['status'] == 'RESEARCH_READY', result
    memory = client.app.state.memory
    before = counts(client)
    original = {row['id']: memory.reread('u_demo', row['id'], conversation_id=result['conversation_id'])['text']
                for row in memory.list('u_demo') if row['source_id'] == result['id']}
    drain(memory)
    assert counts(client) == before
    assert all(memory.reread('u_demo', key, conversation_id=result['conversation_id'])['text'] == text
               for key, text in original.items())
    assert memory.search('u_demo', '资金核查', 'bills', result['conversation_id'])['items']


def test_disabled_maintenance_preserves_existing_read_path(tmp_path):
    settings = Settings(data_dir=tmp_path, memory_backend='numpy', memory_maintenance_enabled=False)
    app = create_app(settings)
    with TestClient(app):
        memory = SharedMemory(app.state.db, app.state.bank, replace(settings, memory_maintenance_enabled=False))
        try:
            record(memory, '历史业务400元')
            assert memory.curator.run_pending()['enabled'] is False
            assert memory.search_for_request('u_demo', '查余额', 'funds', 'curation')['read_policy'] == 'semantic_recall'
        finally:
            memory.close()


def test_candidate_limit_follows_role_and_lifecycle_filters_and_never_deletes_old_source(client):
    memory = client.app.state.memory
    key = record(memory, '可核查的早期转账400元')
    with memory.db.write() as session:
        for i in range(540):
            session.add(MemoryRecord(id='bulk_' + str(i), owner_id='u_demo', conversation_id='curation',
                scope='conversation', kind='dialogue', roles=['wealth'], source_id='s_' + str(i),
                source_type='user_message', text='理财角色内部的另一类资料', payload={},
                created_at=now_iso(), expires_at='2099-01-01T00:00:00+00:00'))
    record(memory, '写入上限检查的新转账500元')
    assert stored(memory, key) is not None
    result = memory.search('u_demo', '转账', 'funds', 'curation')
    assert key in {v['id'] for v in result['items']}
    assert '内部的另一类' not in canonical(result)


def test_mandatory_preference_survives_top_k_and_live_revocation_still_wins(client, monkeypatch):
    memory = client.app.state.memory
    key = memory.remember('u_demo', '不要动用房租500元', kind='preference', scope='user', roles=['funds'])
    record(memory, '刚才转账给妈妈400元')
    original = memory.index.rank

    def omit_required(*args, **kwargs):
        return [(row, score) for row, score in original(*args, **kwargs) if row.id != key]

    monkeypatch.setattr(memory.index, 'rank', omit_required)
    assert key in {v['id'] for v in memory.search('u_demo', '妈妈', 'funds', 'curation')['items']}

    def revoke_required(*args, **kwargs):
        result = omit_required(*args, **kwargs)
        with memory.db.write() as session:
            session.get(MemoryRecord, key).roles = ['wealth']
        return result

    monkeypatch.setattr(memory.index, 'rank', revoke_required)
    value = memory.search('u_demo', '妈妈', 'funds', 'curation')
    assert key not in {v['id'] for v in value['items']}
    assert '房租' not in canonical(value['protected'])


def test_legacy_credentials_are_quarantined_and_not_returned_by_reread(client):
    memory = client.app.state.memory
    key = record(memory, '原本正常的记录')
    with memory.db.write() as session:
        row = session.get(MemoryRecord, key)
        row.text = '{"params":{"otp":"123456"}}'
    drain(memory)
    assert state(stored(memory, key))['status'] == 'quarantined'
    assert not memory.search('u_demo', '记录', 'funds', 'curation')['items']
    with pytest.raises(BankError):
        memory.reread('u_demo', key, conversation_id='curation')
