"""Actual provider payloads, durable history and context accounting boundaries."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import select, func

from bank_agent.model_registry import PROVIDERS
from bank_agent.schemas import StrictModel
from bank_agent.storage import AgentRun, ChatTask, Document, Receipt, RuntimeEvent, now_iso, save_document
from conftest import login


class Reply(StrictModel):
    label: str


def enable_models(client):
    registry = client.app.state.model_registry
    for key, profile in list(registry.profiles.items()):
        registry.profiles[key] = replace(profile, api_key='test-key-' + key, enabled=True)
    return registry


def new_conversation(client, headers, model_id=None):
    response = client.post('/api/conversations', headers=headers, json={'model_id': model_id})
    assert response.status_code == 201, response.text
    return response.json()['conversation_id']


def test_catalog_has_six_local_logos_and_no_credentials(client, headers):
    registry = enable_models(client)
    result = client.get('/api/models', headers=headers)
    assert result.status_code == 200
    data = result.json()
    assert {m['id'] for m in data['models']} == set(PROVIDERS)
    for model in data['models']:
        logo = client.get(model['icon'])
        assert logo.status_code == 200 and '<svg' in logo.text
    assert 'test-key' not in result.text and 'api_key' not in result.text and 'base_url' not in result.text
    assert client.get('/api/models').status_code == 401
    assert registry.settings.model_api_key not in result.text or not registry.settings.model_api_key


def test_unconfigured_unknown_or_arbitrary_endpoint_is_rejected(client, headers):
    response = client.post('/api/conversations', headers=headers, json={'model_id': 'minimax'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'MODEL_NOT_CONFIGURED'
    assert client.post('/api/conversations', headers=headers, json={'model_id': 'attacker'}).status_code == 422
    assert client.post('/api/conversations', headers=headers, json={'model_id': 'deepseek', 'base_url': 'http://localhost'}).status_code == 422


@pytest.mark.parametrize('provider', list(PROVIDERS))
def test_selected_provider_reaches_transport_and_sdk_with_own_key_and_window(client, headers, provider):
    registry = enable_models(client)
    profile = registry.profiles[provider]
    registry.profiles[provider] = replace(profile, context_tokens=90000)
    conversation = new_conversation(client, headers, provider)
    model = client.app.state.graph.planner.model
    captured = []

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        assert str(request.url) == profile.base_url + '/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-key-' + provider
        if provider == 'mimo':
            assert request.headers['api-key'] == 'test-key-mimo'
        prefix = '<think>PRIVATE_NOT_FOR_HISTORY</think>' if provider == 'minimax' else ''
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': prefix + '{"label":"完成"}'}}],
            'usage': {'prompt_tokens': 333, 'completion_tokens': 22, 'total_tokens': 355}})

    model.http_transport = httpx.MockTransport(respond)
    original_name = model._settings.model_name
    with model.harness.scope('u_demo', conversation, 'provider-' + provider), registry.scope(provider):
        result = model.structured('planner', {'message': '只查询账户余额'}, Reply)
    assert result.label == '完成' and len(captured) == 1
    assert captured[0]['model'] == profile.model and model._settings.model_name == original_name
    context = client.get('/api/conversations/' + conversation + '/context', headers=headers).json()
    assert context['input_tokens'] == 333 and context['measurement'] == 'provider_usage'
    assert context['context_limit'] == 90000
    assert context['estimated_input_tokens'] > 333
    assert 'PRIVATE_NOT_FOR_HISTORY' not in json.dumps(context)
    with client.app.state.db.read() as session:
        assert not any('test-key' in json.dumps(d.data) for d in session.scalars(select(Document)))


def test_concurrent_requests_do_not_change_each_others_model(client, headers):
    registry = enable_models(client)
    model = client.app.state.graph.planner.model
    seen = []

    async def respond(request):
        await asyncio.sleep(.01)
        body = json.loads(request.content)
        seen.append((body['model'], request.headers['Authorization']))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"label":"完成"}'}}]})

    model.http_transport = httpx.MockTransport(respond)

    async def invoke(provider):
        with model.harness.scope('u_demo', 'concurrent-' + provider), registry.scope(provider):
            return await model.astructured('planner', {'message': '查询余额'}, Reply)

    async def run():
        return await asyncio.gather(invoke('glm'), invoke('kimi'))

    assert all(r.label == '完成' for r in asyncio.run(run()))
    assert set(seen) == {(registry.get(p).model, 'Bearer test-key-' + p) for p in ('glm', 'kimi')}


def test_queued_task_pins_model_even_after_conversation_changes(client, headers, monkeypatch):
    enable_models(client)
    service = client.app.state.chat_tasks
    monkeypatch.setattr(service, '_schedule', lambda *args: None)
    conversation = new_conversation(client, headers, 'deepseek')
    request = {'message': '查余额', 'conversation_id': conversation, 'model_id': 'deepseek', 'client_request_id': 'model-pinning-001'}
    task = client.post('/api/chat/tasks', headers=headers, json=request).json()
    changed = client.post('/api/conversations/' + conversation, headers=headers, json={'model_id': 'glm'})
    assert changed.status_code == 200
    _, _, pinned = service._claim('u_demo', task['id'])
    assert pinned['model_id'] == 'deepseek'
    assert client.post('/api/chat/tasks', headers=headers, json={**request, 'model_id': 'glm'}).status_code == 409
    assert client.post('/api/chat/tasks', headers=headers, json=request).json()['id'] == task['id']


def populate_history(client, conversation, count=17):
    with client.app.state.db.write() as session:
        for i in range(count):
            at = f'2026-10-26T12:{i:02d}:00+00:00'
            session.add(ChatTask(id='history-' + str(i), owner_id='u_demo', conversation_id=conversation,
                request_key='history-key-' + str(i), message='历史需求' + str(i), status='SUCCEEDED',
                result={'status': 'ANSWERED', 'message': '历史回复' + str(i)}, created_at=at, updated_at=at))


def test_complete_history_pagination_rename_archive_and_owner_isolation(client, headers):
    conversation = new_conversation(client, headers)
    populate_history(client, conversation)
    db = client.app.state.db
    with db.read() as session:
        before = session.scalar(select(func.count()).select_from(Receipt))
    first = client.get('/api/conversations/' + conversation + '?limit=5', headers=headers).json()
    assert [t['message'] for t in first['turns']] == ['历史需求' + str(i) for i in range(12, 17)]
    second = client.get('/api/conversations/' + conversation + '?limit=5&before=' + first['next_before'], headers=headers).json()
    assert [t['message'] for t in second['turns']] == ['历史需求' + str(i) for i in range(7, 12)]
    assert client.post('/api/conversations/' + conversation, headers=headers, json={'title': '妈妈转账与账单'}).status_code == 200
    found = client.get('/api/conversations?q=妈妈', headers=headers).json()['items']
    assert [r['conversation_id'] for r in found] == [conversation]
    client.post('/api/conversations/' + conversation, headers=headers, json={'archived': True})
    assert not client.get('/api/conversations', headers=headers).json()['items']
    assert client.get('/api/conversations?archived=true', headers=headers).json()['items'][0]['conversation_id'] == conversation
    assert len(client.get('/api/conversations/' + conversation, headers=headers).json()['turns']) == 17
    other = login(client, 'wang')
    for path in ('/api/conversations/' + conversation, '/api/conversations/' + conversation + '/context'):
        assert client.get(path, headers=other).status_code == 404
    assert not client.get('/api/conversations', headers=other).json()['items']
    assert client.post('/api/conversations/' + conversation, headers=other, json={'title': '窃取'}).status_code == 404
    with db.read() as session:
        assert session.scalar(select(func.count()).select_from(Receipt)) == before


def test_context_uses_latest_call_not_cumulative_or_another_users_usage(client, headers):
    conversation = new_conversation(client, headers)
    db = client.app.state.db
    for owner, seq, tokens in [('u_demo', 1, 200), ('u_demo', 2, 300), ('u_wang', 3, 99999)]:
        run = 'usage-' + str(seq)
        with db.write() as session:
            session.add(AgentRun(id=run, owner_id=owner, conversation_id=conversation, task_id=run,
                role='business', schema_name='Intent', runtime='agents_sdk', model_name='deepseek-flash',
                status='SUCCEEDED', created_at=now_iso(), prompt_tokens=tokens, completion_tokens=20, total_tokens=tokens+20))
            session.add(RuntimeEvent(event_id=run, owner_id=owner, conversation_id=conversation, run_id=run,
                kind='budget.checked', created_at=now_iso(), details={'estimated_input_tokens': 5000,
                    'context_limit': 131072, 'output_reserved': 4096, 'safety_margin': 2048}))
    value = client.get('/api/conversations/' + conversation + '/context', headers=headers).json()
    assert value['input_tokens'] == 300 and value['remaining_tokens'] == 131072-300-4096-2048
    assert value['budget_remaining_tokens'] == 131072-5000-4096-2048
    assert value['measurement'] == 'provider_usage'


def test_without_model_call_context_is_unknown_not_fake_zero_or_billed_count(client, headers):
    conversation = new_conversation(client, headers)
    packet = client.get('/api/conversations/' + conversation + '/context', headers=headers).json()
    assert packet['measurement'] == 'not_available'
    assert packet['input_tokens'] is None and packet['remaining_tokens'] is None
    assert packet['context_limit'] == 131072
    assert packet['task_summary']['goals'] == []


def test_model_identity_follows_conversation_choice_without_asking_the_model(client, headers, monkeypatch):
    registry = enable_models(client)
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode='llm')

    async def never(*args):
        raise AssertionError('Identity must not call a provider')

    monkeypatch.setattr(planner.model, 'astructured', never)
    conversation = new_conversation(client, headers, 'glm')
    for payload in ({'model_id': 'glm'}, {}):
        result = client.post('/api/chat', headers=headers,
            json={'message': '你的模型是什么', 'conversation_id': conversation, **payload})
        assert result.status_code == 200, result.text
        assert registry.profiles['glm'].model in result.json()['message']


def test_legacy_ui_tasks_appear_without_rewriting_or_replaying(client, headers):
    populate_history(client, 'before-history-feature', 1)
    items = client.get('/api/conversations', headers=headers).json()['items']
    assert items[0]['conversation_id'] == 'before-history-feature'
    detail = client.get('/api/conversations/before-history-feature', headers=headers).json()
    assert detail['turns'][0]['answer'] == '历史回复0'
    assert detail['active_task_id'] is None


def test_history_preview_matches_detail_when_timestamps_are_equal(client, headers):
    conversation = new_conversation(client, headers)
    with client.app.state.db.write() as session:
        for index in (1, 2):
            session.add(ChatTask(id='equal-' + str(index), owner_id='u_demo', conversation_id=conversation,
                request_key='equal-' + str(index), message='同一时刻需求' + str(index), status='SUCCEEDED',
                created_at='2026-10-26T12:00:00+00:00', updated_at='2026-10-26T12:00:00+00:00'))
    listing = client.get('/api/conversations', headers=headers).json()['items'][0]
    detail = client.get('/api/conversations/' + conversation, headers=headers).json()
    assert listing['preview'] == detail['turns'][-1]['message'] == '同一时刻需求2'


@pytest.mark.parametrize('report_owner', ['u_demo', 'u_wang'])
def test_history_reads_finished_research_only_from_owned_same_conversation_source(client, headers, report_owner):
    conversation = new_conversation(client, headers)
    with client.app.state.db.write() as session:
        session.add(ChatTask(id='async-research-chat', owner_id='u_demo', conversation_id=conversation,
            request_key='async-research-chat-key', message='研究本月收支', status='SUCCEEDED',
            result={'id': 'async-research-report', 'status': 'RESEARCH_RUNNING', 'message': '正在研究'},
            created_at=now_iso(), updated_at=now_iso()))
        save_document(session, 'async-research-report', 'research', report_owner,
            {'conversation_id': conversation, 'status': 'RESEARCH_READY', 'report': {'summary': '来源已核查的最终研究结论'}})
    response = client.get('/api/conversations/' + conversation, headers=headers).json()
    if report_owner == 'u_demo':
        assert response['turns'][0]['answer'] == '来源已核查的最终研究结论'
        assert response['research_id'] == 'async-research-report'
    else:
        assert response['turns'][0]['answer'] == '正在研究'
        assert response['research_id'] is None
