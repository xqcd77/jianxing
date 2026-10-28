"""Dated official presets; existing user-selected models remain explicit."""
import pytest

from bank_agent.model_registry import PRESET_MODELS
from bank_agent.runtime_context import TaskContext, task_state


@pytest.mark.parametrize('provider,expected', [
    ('deepseek', 'deepseek-flash'), ('glm', 'glm-5.3'), ('mimo', 'mimo-v2.6-pro'),
    ('kimi', 'kimi-k3.1-pro'), ('qwen', 'qwen3.8-max'), ('minimax', 'MiniMax-M3.1-Flash')])
def test_latest_presets_are_real_model_ids_and_configuration_defaults(client, headers, provider, expected):
    result = client.get('/api/models/' + provider + '/config', headers=headers)
    assert result.status_code == 200
    assert result.json()['model'] == expected
    assert PRESET_MODELS[provider][0] == expected
    assert expected in result.json()['options']
    if provider == 'deepseek':
        assert result.json()['display_model'] == 'DeepSeek-V4.1-Flash-1017'


@pytest.mark.parametrize('provider,model,thinking', [
    ('glm', 'glm-5.3', {'type': 'enabled'}),
    ('kimi', 'kimi-k3.1-pro', None), ('kimi', 'kimi-k3.1-pro-highspeed', None)])
def test_new_thinking_only_models_never_receive_disabled_switch(client, provider, model, thinking):
    token = task_state.set(TaskContext(model_id=provider))
    try:
        request = client.app.state.model_registry.adapt({'model': model, 'max_tokens': 4096,
            'thinking': {'type': 'disabled'}, 'reasoning_effort': 'max'}, 'planner')
    finally:
        task_state.reset(token)
    assert request.get('thinking') == thinking
    assert request['reasoning_effort'] == 'low' and request['max_tokens'] == 4096


def test_custom_saved_model_is_not_silently_replaced_by_new_preset(client, headers):
    response = client.post('/api/models/kimi/config', headers=headers,
        json={'model': 'kimi-k2.5', 'api_key': 'synthetic-personal-key'})
    assert response.status_code == 200
    config = client.get('/api/models/kimi/config', headers=headers).json()
    assert config['model'] == config['display_model'] == 'kimi-k2.5'
    assert 'kimi-k3.1-pro' in config['options']
