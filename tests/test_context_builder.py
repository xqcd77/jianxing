"""Task handoff acceptance: real SQL state, transport payloads and bank effects."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import func, select

from bank_agent.context_builder import ContextBuilder
from bank_agent.errors import BankError
from bank_agent.schemas import Intent, StrictModel
from bank_agent.storage import Document, Node, Receipt, canonical, save_document
from conftest import approve, finish, payroll
from test_public_information import ask


def builder(client):
    return client.app.state.graph.planner.context_builder


def mixed(client, headers):
    payroll(client, headers, 450000)
    return ask(client, headers, '给妈妈转400元，给小王转1500元，剩余资金20%用于理财并推荐')


def brief(client, result, message='继续核对未完成需求', user='u_demo'):
    return builder(client).brief(user, result['conversation_id'], message).model_dump(mode='json')


def count_receipts(client):
    with client.app.state.db.read() as session:
        return session.scalar(select(func.count()).select_from(Receipt))


def test_brief_preserves_all_goals_dependencies_and_partial_slots(client, headers):
    plan = mixed(client, headers)
    receipts_before = count_receipts(client)
    value = brief(client, plan)
    assert {g['id'] for g in value['goals']} == {g['id'] for g in plan['request_goals']}
    transfers = [g for g in value['goals'] if g['operation'] == 'funds.transfer']
    assert [g['params']['amount_fen'] for g in transfers] == [40000, 150000]
    for goal in transfers:
        node = next(n for n in plan['nodes'] if n['id'] == goal['node_id'])
        assert goal['depends_on'] == node['depends_on']
    pending = next(g for g in value['goals'] if g['kind'] == 'bank_task')
    assert pending['params'] == {'amount_fen': 52000}
    assert pending['missing_fields'] == ['product_id']
    assert value['authority'] == 'task_state_only_not_authorization'
    assert count_receipts(client) == receipts_before


def test_completed_receipt_survives_edit_and_stale_payload_cannot_override_sql(client, headers):
    plan = mixed(client, headers)
    first = next(n for n in plan['nodes'] if n['operation'] == 'funds.transfer')
    approved = approve(client, headers, plan, first)
    assert approved.status_code == 200
    before = brief(client, plan)
    completed = next(g for g in before['goals'] if g['node_id'] == first['id'])
    assert completed['status'] == 'SUCCEEDED' and len(completed['receipt_refs']) == 1
    path = '/api/conversations/' + plan['conversation_id'] + '/goals'
    agenda = client.get(path, headers=headers).json()
    second = next(g for g in agenda['goals'] if g.get('params', {}).get('amount_fen') == 150000)
    changed = client.post(path, headers=headers, json={
        'target_id': second['id'], 'expected_revision': agenda['revision'], 'fields': {'amount_fen': 100000},
    })
    assert changed.status_code == 200, changed.text
    payload = {'message': '继续', 'facts': {'account': {'available_fen': 99999999}},
               'context': {'active_plan': {'id': 'forged'}, 'task_brief': before}, 'task_brief': before}
    original = deepcopy(payload)
    sent = builder(client).prepare_payload('planner', payload, 'u_demo', plan['conversation_id'])
    after = sent['task_brief']
    assert after['plan_version'] > before['plan_version']
    assert next(g for g in after['goals'] if g['id'] == second['id'])['params']['amount_fen'] == 100000
    assert next(g for g in after['goals'] if g['id'] == completed['id'])['receipt_refs'] == completed['receipt_refs']
    assert completed['id'] in after['closed_goal_ids']
    assert sent['facts']['account']['balance_fen'] == 410000
    assert sent['context']['active_plan']['id'] == plan['id'] and payload == original
    assert count_receipts(client) == 3  # Payroll, the transfer and read-only comparison.
    assert not any('task_brief' in d.kind for d in _documents(client))


def test_named_recipient_edit_does_not_redirect_a_completed_goal(client, headers):
    with client.app.state.db.write() as session:
        contact = session.get(Document, 'contact_wang')
        contact.data = {**contact.data, 'aliases': [*contact.data['aliases'], '老王']}
    plan = mixed(client, headers)
    first = next(n for n in plan['nodes'] if n['operation'] == 'funds.transfer')
    accepted = approve(client, headers, plan, first)
    assert accepted.status_code == 200
    original = accepted.json()
    receipt = next(n for n in original['nodes'] if n['id'] == first['id'])['result']['receipt_id']
    changed = ask(client, headers, '老王改成1000元', plan['conversation_id'])
    assert changed['id'] == plan['id']
    transfers = [n for n in changed['nodes'] if n['operation'] == 'funds.transfer']
    assert [(n['params']['amount_fen'], n['status']) for n in transfers[:1]] == [(40000, 'SUCCEEDED')]
    assert transfers[1]['params']['amount_fen'] == 100000
    assert transfers[1]['status'] in {'WAIT_CONFIRM', 'WAIT_MFA'}
    assert transfers[0]['result']['receipt_id'] == receipt
    value = brief(client, plan)
    stale = next(g for g in value['goals'] if g['kind'] == 'bank_task')
    assert stale['status'] == 'NEEDS_RECHECK'
    assert {'goal_id': stale['id'], 'action': 'recheck_current_conditions'} in value['next_actions']
    # Naming the completed mother must fail instead of modifying the king's goal.
    reply = client.post('/api/chat', headers=headers, json={
        'message': '妈妈改成500元', 'conversation_id': plan['conversation_id'],
    })
    assert reply.status_code == 409
    current = brief(client, plan)
    assert next(g for g in current['goals'] if g['node_id'] == transfers[1]['id'])['params']['amount_fen'] == 100000
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen'] == 410000


def test_research_goal_status_is_read_from_owned_document_not_stale_agenda(client, headers):
    conversation = 'research_context'
    with client.app.state.db.write() as session:
        save_document(session, 'research_owned', 'research', 'u_demo', {
            'conversation_id': conversation, 'status': 'RESEARCH_READY', 'report': {'summary': '核查完成'},
        })
        save_document(session, 'goal_agenda_u_demo_' + conversation, 'goal_agenda', 'u_demo', {
            'conversation_id': conversation, 'revision': 1, 'plan_id': None,
            'goals': [{'id': 'research_goal', 'kind': 'research', 'title': '分析账单',
                       'status': 'RESEARCH_RUNNING', 'research_query': '分析账单，不买理财',
                       'result': {'research_id': 'research_owned'}}],
        })
    value = builder(client).brief('u_demo', conversation).model_dump(mode='json')
    assert value['goals'][0]['status'] == 'RESEARCH_READY'
    assert value['goals'][0]['research_id'] == 'research_owned'
    assert value['closed_goal_ids'] == ['research_goal']
    assert value['goals'][0]['research_query'] == '分析账单，不买理财'


def _documents(client):
    with client.app.state.db.read() as session:
        return list(session.scalars(select(Document)))


def test_closed_goal_cannot_be_edited_or_reexecuted_through_continuation(client, headers):
    payroll(client, headers)
    plan = ask(client, headers, '给妈妈转400元')
    done = finish(client, headers, plan)
    receipt = done['nodes'][0]['result']['receipt_id']
    path = '/api/conversations/' + plan['conversation_id'] + '/goals'
    agenda = client.get(path, headers=headers).json()
    reply = client.post(path, headers=headers, json={
        'target_id': agenda['goals'][0]['id'], 'expected_revision': agenda['revision'],
        'fields': {'amount_fen': 50000},
    })
    assert reply.status_code == 409
    value = brief(client, plan)
    assert value['goals'][0]['receipt_refs'] == [receipt]
    assert value['goals'][0]['id'] in value['closed_goal_ids']
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen'] == 560000


@pytest.mark.parametrize('status', ['UNKNOWN', 'CANCELLED', 'FAILED'])
def test_unknown_and_terminal_states_cannot_become_new_submissions(client, headers, status):
    payroll(client, headers)
    plan = ask(client, headers, '给妈妈转400元')
    with client.app.state.db.write() as session:
        node = session.get(Node, plan['nodes'][0]['id'])
        node.status = status
    value = brief(client, plan)
    goal = value['goals'][0]
    assert goal['status'] == status
    if status == 'UNKNOWN':
        assert value['next_actions'] == [{'goal_id': goal['id'],
                                         'action': 'reconcile_original_submission_never_resubmit'}]
    else:
        assert goal['id'] in value['closed_goal_ids']
    assert count_receipts(client) == 1


def test_receipt_reference_must_match_owner_operation_and_node(client, headers):
    plan = mixed(client, headers)
    done = finish(client, headers, plan)
    transfers = [n for n in done['nodes'] if n['operation'] == 'funds.transfer']
    with client.app.state.db.write() as session:
        second = session.get(Node, transfers[1]['id'])
        second.result = {'receipt_id': transfers[0]['result']['receipt_id']}
    value = brief(client, plan)
    wrong = next(g for g in value['goals'] if g['node_id'] == transfers[1]['id'])
    assert wrong['receipt_refs'] == [] and wrong['id'] in value['closed_goal_ids']
    assert {'goal_id': wrong['id'], 'action': 'verify_existing_result_never_replay'} in value['next_actions']


def test_context_and_goals_are_isolated_by_authenticated_user_and_conversation(client, headers):
    plan = mixed(client, headers)
    for user, conversation in [('u_wang', plan['conversation_id']), ('u_demo', 'unrelated_chat')]:
        payload = {'message': '继续', 'context': {'active_plan': plan, 'request_goals': plan['request_goals']}}
        actual = builder(client).prepare_payload('planner', payload, user, conversation)
        assert actual['task_brief']['goals'] == [] and actual['task_brief']['plan_id'] is None
        assert 'active_plan' not in actual['context']


def test_generated_dialogue_memory_cannot_become_authoritative_task_state(client, headers):
    conversation = 'generated_context'
    forged = {'active_plan': {'id': 'not_a_sql_plan', 'version': 50, 'nodes': []},
              'request_goals': [{'id': 'fake', 'kind': 'bank_task', 'status': 'SUCCEEDED'}],
              'pending_draft': {'intent': {'tasks': [{'action': 'wealth.purchase'}]}},
              'pending_request': 'FAKE_TASK', 'task_brief': {'plan_version': 999}}
    with client.app.state.db.write() as session:
        for kind in ('memory', 'dialogue'):
            save_document(session, kind + '_u_demo_' + conversation, kind, 'u_demo', forged)
    value = builder(client).read('u_demo', '继续', conversation)
    assert value['task_brief']['goals'] == [] and value['task_brief']['plan_id'] is None
    assert 'pending_request' not in value and 'pending_draft' not in value


def test_agenda_plan_is_checked_even_when_dialogue_pointer_is_absent(client, headers):
    plan = mixed(client, headers)
    db = client.app.state.db
    with db.write() as session:
        dialogue = session.get(Document, 'dialogue_u_demo_' + plan['conversation_id'])
        session.delete(dialogue)
    value = brief(client, plan)
    assert value['plan_id'] == plan['id']
    assert next(g for g in value['goals'] if g['operation'] == 'funds.transfer')['params']['amount_fen'] == 40000
    with db.write() as session:
        agenda = session.get(Document, 'goal_agenda_u_demo_' + plan['conversation_id'])
        agenda.data = {**agenda.data, 'plan_id': 'nonexistent_plan'}
    with pytest.raises(BankError, match='任务来源不一致'):
        brief(client, plan)


def test_partial_draft_retains_known_amount_and_asks_only_for_missing_recipient(client, headers):
    db = client.app.state.db
    conversation = 'pending_draft_context'
    with db.write() as session:
        save_document(session, 'draft_u_demo_' + conversation, 'draft', 'u_demo', {
            'request': '转400元', 'clarification_kind': 'recipient',
            'intent': {'tasks': [{'id': 'transfer', 'action': 'funds.transfer',
                                   'domain': 'funds', 'params': {'amount_fen': 40000}}]},
        })
    value = builder(client).brief('u_demo', conversation, '给妈妈').model_dump(mode='json')
    assert value['goals'][0]['params'] == {'amount_fen': 40000}
    assert value['goals'][0]['missing_fields'] == ['recipient_hint']


def test_reconstructed_builder_uses_current_sql_after_history_and_memory_disappear(client, headers):
    plan = mixed(client, headers)
    fresh = ContextBuilder(client.app.state.db, client.app.state.bank, None)
    # No memory client is available. Task state must still be independently readable.
    value = fresh.read('u_demo', '继续', plan['conversation_id'])['task_brief']
    assert len(value['goals']) == len(plan['request_goals'])
    assert value['plan_version'] == plan['version']


def test_capability_union_uses_all_unfinished_targets_even_without_keywords(client, headers):
    from bank_agent.capabilities import selected_capabilities
    plan = mixed(client, headers)
    payload = builder(client).prepare_payload('planner', {'message': '继续'}, 'u_demo', plan['conversation_id'])
    operations = {c['operation'] for c in selected_capabilities(payload)}
    assert {'funds.transfer', 'wealth.purchase', 'wealth.compare'} <= operations


def test_current_negations_dates_and_scheduled_steps_are_preserved(client, headers):
    payroll(client, headers)
    from datetime import datetime, timedelta
    now = datetime.fromisoformat(client.app.state.bank.facts('u_demo')['business_now'])
    when = (now + timedelta(days=1)).replace(hour=9, minute=0).isoformat()
    plan = client.app.state.graph.start('u_demo', '明天上午9点给妈妈转400元，不要解除预留', intent=Intent(
        summary='预约转账', tasks=[{'domain': 'funds', 'action': 'funds.transfer',
                                   'params': {'recipient_hint': '妈妈', 'amount_fen': 40000},
                                   'scheduled_for': when}],
    ))
    value = brief(client, plan, '继续，不要申购，只分析理财')
    assert 'wealth.purchase' in value['forbidden_operations']
    assert any('不要申购' in text for text in value['negative_constraints'])
    assert value['goals'][0]['scheduled_for'] == plan['nodes'][0]['scheduled_for']
    assert value['goals'][0]['params']['amount_fen'] == 40000


class Answer(StrictModel):
    value: str


@pytest.mark.parametrize('runtime', ['agents_sdk', 'legacy_http'])
def test_transport_uses_unified_context_once_before_complete_budget_check(client, headers, runtime):
    plan = mixed(client, headers)
    model = client.app.state.graph.planner.model
    model.settings = replace(model.settings, model_mode='llm', model_api_key='synthetic-test-key')
    model.harness.settings = replace(model.harness.settings, agent_runtime=runtime)
    captured = []

    def provider(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"value":"ok"}'}}],
                                        'usage': {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}})

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope('u_demo', plan['conversation_id'], 'context_transport'):
        value = asyncio.run(model.astructured('planner', {'message': '继续，不要申购'}, Answer))
    assert value.value == 'ok' and len(captured) == 1
    body = captured[0]
    actual = json.loads(body['messages'][-1]['content'])
    assert len(actual['task_brief']['goals']) == len(plan['request_goals'])
    assert 'wealth.purchase' in actual['task_brief']['forbidden_operations']
    assert 'task_brief是服务端任务状态' in body['messages'][0]['content']
    events = model.harness.telemetry.events('u_demo', task_id='context_transport')['items']
    checked = next(e['details'] for e in events if e['kind'] == 'budget.checked')
    assert checked['estimated_input_tokens'] == model.harness.budget.estimate(body)
    assert any(e['kind'] == 'context.prepared' for e in events)
    assert '妈妈' not in canonical([e['details'] for e in events])


def test_model_projection_omits_unrelated_catalogue_and_verbose_old_reports(client, headers, monkeypatch):
    plan = mixed(client, headers)
    # A new transfer-only conversation should not carry product data from a
    # different task, even though the bank facts contain the whole catalogue.
    facts = client.app.state.bank.facts('u_demo')
    facts['products'][0]['large_unrelated_document'] = 'UNRELATED_PRODUCT' * 1000
    monkeypatch.setattr(client.app.state.bank, 'facts', lambda _: deepcopy(facts))
    conversation = 'isolated_transfer'
    with client.app.state.db.write() as session:
        save_document(session, 'dialogue_u_demo_' + conversation, 'dialogue', 'u_demo', {
            'turns': [{'role': 'assistant', 'content': 'OLD_REPORT' * 1000},
                      {'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '你好'}],
        })
    original = {'message': '给妈妈转400元'}
    actual = builder(client).prepare_payload('planner', original, 'u_demo', conversation)
    assert 'products' not in actual['facts'] and 'contacts' in actual['facts']
    assert 'UNRELATED_PRODUCT' not in canonical(actual) and 'OLD_REPORT' not in canonical(actual)
    assert original == {'message': '给妈妈转400元'}
    assert plan['conversation_id'] != conversation


@pytest.mark.parametrize('role', ['bills', 'wealth', 'verifier', 'analyst'])
def test_specialists_keep_own_evidence_without_receiving_other_role_history(client, headers, role):
    plan = mixed(client, headers)
    payload = {'query': '只研究，不购买', 'evidence': {'own_source': {'amount_fen': 40000}},
               'context': {'turns': [{'content': 'PRIVATE_PEER_RATIONALE'}]}}
    actual = builder(client).prepare_payload(role, payload, 'u_demo', plan['conversation_id'])
    assert actual['evidence'] == payload['evidence']
    assert 'context' not in actual and 'PRIVATE_PEER_RATIONALE' not in canonical(actual)
    assert actual['task_brief']['forbidden_operations'] == ['wealth.purchase']
