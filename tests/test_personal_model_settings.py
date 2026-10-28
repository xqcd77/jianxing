"""User keys are private, encrypted, versioned and actually used by transports."""
from dataclasses import replace
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from bank_agent.model_registry import ModelRegistry
from bank_agent.runtime_context import task_state, TaskContext
from bank_agent.schemas import StrictModel
from bank_agent.storage import Document
from conftest import login


KEY = 'test-personal-glm-key-12345'


class Reply(StrictModel):
    label: str


def configure(client, headers, key=KEY, model='glm-4.7-flashx', provider='glm'):
    value = {'model': model}
    if key is not None:
        value['api_key'] = key
    return client.post('/api/models/' + provider + '/config', headers=headers, json=value)


def test_every_provider_has_editable_configuration_and_concrete_choices(client, headers):
    models = client.get('/api/models', headers=headers).json()['models']
    assert len(models) == 6
    for model in models:
        response = client.get('/api/models/' + model['id'] + '/config', headers=headers)
        assert response.status_code == 200
        assert model['model'] in response.json()['options']
        assert 'api_key' not in response.text and 'ciphertext' not in response.text
    assert client.get('/api/models/glm/config').status_code == 401


def test_key_encrypted_at_rest_never_returned_and_scoped_to_owner(client, headers):
    result = configure(client, headers)
    assert result.status_code == 200, result.text
    assert result.json()['has_personal_key'] and result.json()['model'] == 'glm-4.7-flashx'
    assert KEY not in result.text
    own = client.get('/api/models', headers=headers)
    assert next(m for m in own.json()['models'] if m['id'] == 'glm')['enabled']
    assert KEY not in own.text
    other = login(client, 'wang')
    foreign = client.get('/api/models/glm/config', headers=other).json()
    assert not foreign['has_personal_key'] and not foreign['enabled']
    assert foreign['model'] == 'glm-5.3'
    db = client.app.state.db
    with db.read() as session:
        docs = list(session.scalars(select(Document)))
        assert any(d.kind == 'model_credential' for d in docs)
        assert KEY not in json.dumps([d.data for d in docs])
    assert KEY.encode() not in db.path.read_bytes()
    registry = client.app.state.model_registry
    reopened = ModelRegistry(registry.settings, db, client.app.state.auth.fernet)
    assert reopened.get('glm', owner_id='u_demo').api_key == KEY


def test_private_model_and_key_reach_the_actual_sdk_request(client, headers):
    assert configure(client, headers).status_code == 200
    registry, model = client.app.state.model_registry, client.app.state.graph.planner.model
    seen = []

    def provider(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers['authorization'] == 'Bearer ' + KEY
        assert body['model'] == 'glm-4.7-flashx' and KEY not in request.content.decode()
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"label":"完成"}'}}]})

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope('u_demo', 'personal-key-test'), registry.scope('glm', owner_id='u_demo'):
        assert model.structured('planner', {'message': '查询余额'}, Reply).label == '完成'
    assert len(seen) == 1
    assert registry.profiles['glm'].api_key == ''  # Shared adapter configuration is untouched.


def test_concurrent_accounts_use_their_own_key_for_the_same_provider(client, headers):
    assert configure(client, headers).status_code == 200
    other = login(client, 'wang')
    assert configure(client, other, key='test-wang-private-key-98765', model='glm-4.7').status_code == 200
    registry, model = client.app.state.model_registry, client.app.state.graph.planner.model
    seen = []

    async def provider(request):
        await asyncio.sleep(.01)
        seen.append((json.loads(request.content)['model'], request.headers['authorization']))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"label":"完成"}'}}]})

    model.http_transport = httpx.MockTransport(provider)

    async def run(owner):
        with model.harness.scope(owner, 'own-' + owner), registry.scope('glm', owner_id=owner):
            return await model.astructured('planner', {'message': '查询余额'}, Reply)

    async def both():
        return await asyncio.gather(run('u_demo'), run('u_wang'))

    assert all(result.label == '完成' for result in asyncio.run(both()))
    assert set(seen) == {('glm-4.7-flashx', 'Bearer ' + KEY), ('glm-4.7', 'Bearer test-wang-private-key-98765')}


def test_replace_key_keeps_running_and_queued_requests_on_original_version(client, headers, monkeypatch):
    registry = client.app.state.model_registry
    assert configure(client, headers).status_code == 200
    conversation = client.post('/api/conversations', headers=headers, json={'model_id': 'glm'}).json()['conversation_id']
    service = client.app.state.chat_tasks
    monkeypatch.setattr(service, '_schedule', lambda *args: None)
    task = client.post('/api/chat/tasks', headers=headers, json={'message': '查询余额', 'conversation_id': conversation,
        'model_id': 'glm', 'client_request_id': 'pinned-personal-key-001'}).json()
    _, _, pinned = service._claim('u_demo', task['id'])
    assert pinned['credential_version'] == 1
    assert configure(client, headers, key='replacement-personal-key-67890', model='glm-4.7').status_code == 200
    token = task_state.set(TaskContext(owner_id='u_demo', model_id='glm', model_name=pinned['model_name'],
        model_credential_version=pinned['credential_version']))
    try:
        actual = registry.effective(registry.settings)
        assert actual.model_api_key == KEY and actual.model_name == 'glm-4.7-flashx'
    finally:
        task_state.reset(token)
    with registry.scope('glm', owner_id='u_demo'):
        assert registry.effective(registry.settings).model_api_key == 'replacement-personal-key-67890'


def test_blank_key_keeps_personal_key_and_can_change_model(client, headers):
    assert configure(client, headers).status_code == 200
    assert configure(client, headers, key=None, model='glm-4.7').status_code == 200
    registry = client.app.state.model_registry
    profile = registry.get('glm', owner_id='u_demo')
    assert profile.api_key == KEY and profile.model == 'glm-4.7' and profile.credential_version == 1


@pytest.mark.parametrize('value', [
    {'api_key': KEY}, {'model': 'invalid model', 'api_key': KEY},
    {'model': 'glm-4.7', 'api_key': KEY, 'owner_id': 'u_wang'},
    {'model': 'glm-4.7', 'api_key': 'test-key\r\nAuthorization: injected'},
])
def test_validation_errors_never_echo_key_payload(client, headers, value):
    result = client.post('/api/models/glm/config', headers=headers, json=value)
    assert result.status_code == 422
    assert KEY not in result.text and 'injected' not in result.text and 'input' not in result.text


def test_fetch_available_models_uses_supplied_key_and_does_not_save_it(client, headers):
    registry = client.app.state.model_registry

    def provider(request):
        assert str(request.url) == 'https://open.bigmodel.cn/api/paas/v4/models'
        assert request.headers['authorization'] == 'Bearer ' + KEY
        return httpx.Response(200, json={'data': [{'id': 'glm-4.7'}, {'id': 'glm-4.7-flashx'},
            {'id': '<script>alert(1)</script>'}, {'id': 'glm-4.7'}]})

    registry.discovery_transport = httpx.MockTransport(provider)
    result = client.post('/api/models/glm/options', headers=headers, json={'api_key': KEY})
    assert result.status_code == 200
    assert result.json()['models'] == ['glm-4.7', 'glm-4.7-flashx'] and KEY not in result.text
    assert not client.get('/api/models/glm/config', headers=headers).json()['has_personal_key']


def test_discovery_failures_do_not_return_provider_body_or_keys(client, headers):
    registry = client.app.state.model_registry
    registry.discovery_transport = httpx.MockTransport(lambda request: httpx.Response(401, text='Invalid key ' + KEY))
    result = client.post('/api/models/glm/options', headers=headers, json={'api_key': KEY})
    assert result.status_code == 502 and KEY not in result.text


def test_server_credentials_stay_hidden_when_only_changing_model(client, headers):
    registry = client.app.state.model_registry
    registry.profiles['deepseek'] = replace(registry.profiles['deepseek'], api_key='server-default-hidden-key', enabled=True)
    response = configure(client, headers, key=None, model='deepseek-v4-pro', provider='deepseek')
    assert response.status_code == 200 and response.json()['credential_source'] == 'server'
    assert not response.json()['has_personal_key'] and 'server-default-hidden-key' not in response.text
