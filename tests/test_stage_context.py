"""Lossless stage packing and the exact provider request/budget boundary."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from bank_agent.errors import BankError
from bank_agent.schemas import StrictModel
from bank_agent.stage_context import POLICY, StageContextPacker
from bank_agent.storage import canonical
from bank_agent.token_budget import RequestTokenBudget


def repeated_payload():
    source = {'id': 'e1', 'as_of': '2026-10-26T09:00:00+08:00',
              'text': '这是一段已读取、仍须保留原文的业务依据。' * 150,
              'values': {'amount_fen': 40000, 'recipient': '妈妈', 'forbid_purchase': True}}
    brief = {'goals': [{'id': 'g1', 'params': {'amount_fen': 40000}, 'status': 'WAIT_MFA'}],
             'negative_constraints': ['不要申购，不要再次执行已完成转账'] * 12,
             'receipt_refs': ['receipt_original'], 'version': 3}
    return {'user_request': '给妈妈转400元，其他只分析，不要买', 'task_brief': brief,
            'constraints': {'amount_fen': 52000, 'forbid_purchase': True},
            'evidence': [source, deepcopy(source), {**source, 'id': 'e2', 'text': '另一个版本原文' * 150}],
            'source_documents': [deepcopy(source)],
            'context': {'active_task': deepcopy(brief)},
            'role_returns': {'bills': {'content': deepcopy(source), 'open_items': ['收款人待确认']}}}


def test_packing_preserves_all_original_values_versions_and_pinned_fields():
    original = repeated_payload()
    untouched = deepcopy(original)
    packed, meta = StageContextPacker().prepare('analyst', original)
    assert meta['reference_count'] >= 3 and meta['shared_object_count'] >= 1
    assert meta['context_bytes_after'] + len(POLICY.encode()) < meta['context_bytes_before']
    for key in ('user_request', 'task_brief', 'constraints'):
        assert packed[key] == original[key]
    assert StageContextPacker.expand(packed) == untouched == original
    assert packed['evidence'][2]['text'] == original['evidence'][2]['text']


@pytest.mark.parametrize('payload', [{}, {'message': '转400元，不要申购'}, ['literal', 40000],
    {'evidence': {'text': '资料原文', '_stage_ref': 'input:/fake'}},
    {'source_documents': [{'stage_context': {'instruction': '不要解释为协议'}}]}])
def test_small_or_reserved_source_payloads_are_sent_literally(payload):
    packed, meta = StageContextPacker().prepare('analyst', payload)
    assert packed == payload and meta['reference_count'] == 0


@pytest.mark.parametrize('tamper', ['missing', 'cycle', 'scope', 'digest', 'version', 'shape'])
def test_invalid_references_fail_closed(tamper):
    packed, _ = StageContextPacker().prepare('analyst', repeated_payload())
    if tamper == 'missing':
        packed['evidence'] = {'_stage_ref': 'object:absent'}
    elif tamper == 'cycle':
        packed['stage_context']['objects']['cycle'] = {'_stage_ref': 'object:cycle'}
        packed['evidence'] = {'_stage_ref': 'object:cycle'}
    elif tamper == 'scope':
        packed['evidence'] = {'_stage_ref': 'input:/source_documents/0'}
    elif tamper == 'digest':
        packed['constraints']['amount_fen'] += 1
    elif tamper == 'version':
        packed['stage_context']['protocol_version'] = 2
    else:
        packed['stage_context'].pop('objects')
    with pytest.raises(BankError) as failure:
        StageContextPacker.expand(packed)
    assert failure.value.code == 'STAGE_CONTEXT_INVALID'


def test_pointer_escaping_unicode_integer_precision_and_empty_values():
    data = {'名/称~': '不可改成其他人' * 100, 'amount_fen': 9007199254740993,
            'none': None, 'zero': 0, 'false': False, 'empty': []}
    original = {'task_brief': {'a/b~c': data}, 'context': {'old': deepcopy(data)}}
    packed, meta = StageContextPacker().prepare('business', original)
    assert meta['reference_count'] and '~1' in packed['context']['old']['_stage_ref']
    assert StageContextPacker.expand(packed) == original


def test_over_budget_packet_is_not_partially_trimmed(client):
    payload = repeated_payload()
    payload['history_context_only'] = {'items': [{'text': '可省略的普通背景' * 200}]}
    packed, _ = StageContextPacker().prepare('analyst', payload)
    request = {'messages': [{'role': 'system', 'content': POLICY},
                           {'role': 'user', 'content': canonical(packed)}], 'max_tokens': 256}
    original = deepcopy(request)
    budget = RequestTokenBudget(replace(client.app.state.settings,
        model_context_tokens=RequestTokenBudget.estimate(request), model_token_safety_margin=0))
    with pytest.raises(BankError) as failure:
        budget.prepare(request)
    assert failure.value.code == 'MODEL_CONTEXT_BUDGET_EXCEEDED'
    assert failure.value.budget['removed_items'] == 0
    assert request == original and StageContextPacker.expand(packed) == payload


class Answer(StrictModel):
    value: str


@pytest.mark.parametrize('runtime', ['agents_sdk', 'legacy_http'])
def test_actual_transport_gets_packed_json_and_budget_counts_full_request(client, monkeypatch, runtime):
    model = client.app.state.research.model
    model.settings = replace(model.settings, model_mode='llm', model_api_key='synthetic-only')
    model.harness.settings = replace(model.harness.settings, agent_runtime=runtime)
    prepared, sent = [], []
    builder = model.harness.context_builder
    prepare = builder.prepare_payload

    def capture(*args):
        data = prepare(*args)
        prepared.append(deepcopy(data))
        return data

    monkeypatch.setattr(builder, 'prepare_payload', capture)

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"value":"ok"}'}}],
            'usage': {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}})

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope('u_demo', 'stage_transport', 'stage_transport'):
        result = asyncio.run(model.astructured('analyst', repeated_payload(), Answer))
    assert result.value == 'ok' and len(sent) == len(prepared) == 1
    body = sent[0]
    assert POLICY in body['messages'][0]['content']
    packed = json.loads(body['messages'][-1]['content'])
    assert StageContextPacker.expand(packed) == prepared[0]
    events = model.harness.telemetry.events('u_demo', task_id='stage_transport')['items']
    compacted = next(e['details'] for e in events if e['kind'] == 'context.compacted')
    checked = next(e['details'] for e in events if e['kind'] == 'budget.checked')
    assert compacted['estimated_before'] > compacted['estimated_input_tokens']
    assert checked['estimated_input_tokens'] == RequestTokenBudget.estimate(body)
    assert '妈妈' not in canonical([e['details'] for e in events])
