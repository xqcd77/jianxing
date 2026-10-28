"""Handoff isolation, typed evidence, failure retention and real graph boundaries."""
from copy import deepcopy
import asyncio

from pydantic import ValidationError
import pytest
from sqlalchemy import select

from bank_agent.errors import BankError
from bank_agent.role_returns import RoleReturn, RoleReturnStore
from bank_agent.research_tools import digest
from bank_agent.runtime_context import TaskContext, task_state
from bank_agent.schemas import Intent
from bank_agent.storage import Database, Document, canonical
from conftest import payroll
from test_research import counts, research


@pytest.fixture
def store(tmp_path):
    return RoleReturnStore(Database(tmp_path))


def packet_args():
    data = {'amount_fen': 40000, 'recipient': '妈妈', 'forbid_purchase': True}
    return dict(owner_id='u_demo', conversation_id='c1', task_id='r1', role='bills_research',
        stage='specialist_research', revision=2, source_fingerprint='fresh_bank_state', status='partial',
        content={'kind': 'specialist', 'claims': [{'id': 'bills:c1', 'kind': 'fact', 'text': '只分析，不要买',
            'values': data, 'evidence_ids': ['e1']}], 'focus_claim_ids': ['bills:c1']},
        evidence=[{'id': 'e1', 'data': data, 'sha256': digest(data), 'as_of': '2026-10-26T09:00:00+08:00',
                   'source_ids': ['raw_original_1']}], open_items=['还有一笔收款人待补充'], next_reads=['transaction_details'])


def read_args(**extra):
    return dict(owner_id='u_demo', conversation_id='c1', task_id='r1', reader='supervisor', **extra)


def test_typed_packet_retains_missing_numbers_negation_and_is_immutable(store):
    args = packet_args()
    return_id = store.record(**args)
    assert store.record(**args) == return_id
    args['content']['claims'][0]['values']['amount_fen'] = 1
    packet = store.read(return_id, **read_args())
    assert packet.content.claims[0].values['amount_fen'] == 40000
    assert packet.open_items and packet.next_reads == ['transaction_details']
    assert packet.authority == 'proposal_or_read_only_not_authorization'
    assert packet.evidence_refs[0].source_ids == ['raw_original_1']
    assert 'owner_id' not in store.model_input(return_id, **read_args())


@pytest.mark.parametrize('field,value', [('owner_id', 'u_wang'), ('conversation_id', 'another'),
                                      ('reader', 'wealth_research'), ('task_id', 'other_task')])
def test_owner_conversation_role_and_task_isolation(store, field, value):
    return_id = store.record(**packet_args())
    scope = read_args()
    scope[field] = value
    with pytest.raises(BankError):
        store.read(return_id, **scope)


def test_bound_actor_cannot_impersonate_another_owner(store):
    return_id = store.record(**packet_args())
    token = task_state.set(TaskContext(owner_id='u_wang', conversation_id='c1'))
    try:
        with pytest.raises(BankError):
            store.read(return_id, **read_args())
        with pytest.raises(BankError):
            store.record(**packet_args())
    finally:
        task_state.reset(token)


@pytest.mark.parametrize('bad', ['rationale', 'evidence', 'tool', 'claim', 'role'])
def test_bad_private_or_unbound_role_outputs_are_rejected(store, bad):
    args = packet_args()
    if bad == 'rationale':
        args['content']['rationale'] = 'PRIVATE_REASONING'
    elif bad == 'evidence':
        args['content']['claims'][0]['evidence_ids'] = ['never_read']
    elif bad == 'tool':
        args['next_reads'] = ['wealth.purchase']
    elif bad == 'claim':
        args['content']['focus_claim_ids'] = ['forged_claim']
    else:
        args['role'] = 'business'
    with pytest.raises((ValidationError, BankError)):
        store.record(**args)


def test_changed_source_and_changed_stored_packet_fail_closed(store):
    args = packet_args()
    broken = deepcopy(args)
    broken['evidence'][0]['data']['amount_fen'] = 1
    with pytest.raises(BankError) as error:
        store.record(**broken)
    assert error.value.code == 'ROLE_RETURN_EVIDENCE'
    return_id = store.record(**args)
    with store.db.write() as session:
        row = session.get(Document, return_id)
        row.data = {**row.data, 'status': 'complete'}
    with pytest.raises(BankError) as error:
        store.read(return_id, **read_args())
    assert error.value.code == 'ROLE_RETURN_CHANGED'


def stored(client):
    with client.app.state.db.read() as session:
        return [RoleReturn.model_validate(row.data) for row in session.scalars(
            select(Document).where(Document.kind == 'role_return'))]


def test_research_graph_handoffs_keep_sources_and_missing_without_money_writes(client, headers):
    payroll(client, headers)
    before = counts(client)
    result = research(client, headers, {'analysis_only': True, 'forbid_purchase': True},
        query='分析收支与理财，只研究，不要买')
    assert result['status'] == 'RESEARCH_READY', result
    assert counts(client) == before
    packets = [p for p in stored(client) if p.task_id == result['id']]
    assert {p.role for p in packets} == {'supervisor', 'bills_research', 'wealth_research', 'verifier'}
    assert any(p.content.kind == 'research_brief' and p.content.brief.forbid_purchase for p in packets)
    assert any(p.status == 'partial' and p.open_items for p in packets)
    assert all('rationale' not in canonical(p.model_dump()) for p in packets)
    with client.app.state.db.read() as session:
        assert session.get(Document, result['id']).data['role_return_ids']
    assert 'role_return_ids' not in result  # internal protocol stays outside UI
    for role in ('bills_research', 'wealth_research'):
        own = [p for p in packets if p.role == role]
        assert own and all(p.evidence_refs for p in own)
        assert all(c.id.startswith(role.removesuffix('_research') + ':') for p in own for c in p.content.claims)
    for ev in result['report']['evidence']:
        response = client.get(f"/api/research/{result['id']}/evidence/{ev['id']}", headers=headers)
        assert response.status_code == 200


def test_business_packet_tracks_compiled_nodes_and_does_not_replace_authorization(client, headers, monkeypatch):
    payroll(client, headers)
    before = counts(client)
    captured = []
    review = client.app.state.graph.semantic_verifier.review

    async def capture(payload):
        captured.append(deepcopy(payload))
        return await review(payload)

    monkeypatch.setattr(client.app.state.graph.semantic_verifier, 'review', capture)
    plan = client.post('/api/chat', headers=headers, json={'message': '给妈妈转400元，再预留生活费500元'}).json()
    assert plan['status'] in {'WAIT_CONFIRM', 'WAIT_MFA'}, plan
    assert captured and captured[0]['role_returns']['business']['content']['nodes'] == captured[0]['candidate_nodes']
    packets = stored(client)
    assert {'supervisor', 'business', 'verifier'} <= {p.role for p in packets}
    after = counts(client)
    assert before[1] == after[1]  # creating a proposal does not debit money
    assert all(n['status'] != 'SUCCEEDED' for n in plan['nodes'] if n['tier'] != 'GREEN')


def test_other_goals_remain_explicitly_partial_with_titles_and_next_conditions(client):
    intent = Intent(summary='转账后理财并开会员', additional_goals=[
        {'kind': 'bank_task', 'title': '理财待选产品', 'message': '请先选择产品，不代你购买',
         'source_text': '剩下的钱20%用于理财',
         'pending_task': {'domain': 'wealth', 'action': 'wealth.purchase', 'params': {'amount_fen': 52000}}},
        {'kind': 'capability_gap', 'title': '会员服务待接入'},
    ])
    graph = client.app.state.graph
    state = {'user_id': 'u_demo', 'conversation_id': 'remaining_goals', 'thread_id': 'remaining_thread'}
    result = asyncio.run(graph._planned_return(state, {'intent': intent.model_dump(mode='json')}))
    packet = client.app.state.harness.role_returns.read(result['role_returns']['business:business_planning'],
        owner_id='u_demo', conversation_id='remaining_goals', task_id='remaining_thread', reader='supervisor')
    assert packet.status == 'partial'
    assert packet.open_items == ['请先选择产品，不代你购买', '会员服务待接入']
    assert packet.content.intent.additional_goals[0].pending_task.params['amount_fen'] == 52000
