import time
import httpx
import pytest
from fastapi.testclient import TestClient
from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.storage import Document
from conftest import login


@pytest.mark.parametrize('statuses,expected,calls', [([503,200],200,2), ([503,503],502,2), ([401],502,1)])
def test_planner_retries_only_transient_requests_before_any_financial_action(tmp_path, statuses, expected, calls):
    app = create_app(Settings(data_dir=tmp_path, model_mode='llm', model_api_key='synthetic-test-key', scheduler_mode='disabled'))
    seen = []
    def provider(request):
        seen.append(request)
        status = statuses[min(len(seen)-1,len(statuses)-1)]
        return httpx.Response(status, json={'choices':[{'finish_reason':'stop','message':{'content':'{"summary":"查询余额","tasks":[{"domain":"funds","action":"balance.query","params":{}}]}'}}]})
    app.state.graph.planner.model.http_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        headers = login(client)
        result = client.post('/api/chat', headers=headers, json={'message':'我的账户还有多少钱？'})
        assert result.status_code == expected, result.text
        assert len(seen) == calls
        assert client.get('/api/overview',headers=headers).json()['account']['balance_fen'] == 0


@pytest.mark.parametrize('query',['2026年13月账单','2026-00账单'])
def test_invalid_explicit_month_never_defaults_to_current_month(query):
    from bank_agent.dialogue import period_slot
    from bank_agent.errors import ClarificationNeeded
    with pytest.raises(ClarificationNeeded):
        period_slot(query, '2026-10-15T12:00:00+08:00')


def test_known_lost_card_unlock_does_not_need_model_guessing(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode='llm', scheduler_mode='disabled'))
    async def forbidden(*args):
        raise AssertionError('Explicit verified card operation must not call the model')
    app.state.graph.planner.model.astructured = forbidden
    with app.state.db.write() as session:
        card = session.get(Document, 'C_MAIN')
        card.data = {**card.data, 'status': 'LOST'}
    with TestClient(app) as client:
        headers = login(client)
        result = client.post('/api/chat', headers=headers, json={'message':'解除默认演示卡的交易限制，不要解挂'}).json()
        assert result['nodes'][0]['operation'] == 'cards.unlock'
        assert result['nodes'][0]['error']['code'] == 'CARD_STATE_CONFLICT'
        assert result['status'] == 'FAILED'


def test_supervisor_schema_repair_is_bounded_and_keeps_original_constraints(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode='llm', scheduler_mode='disabled'))
    calls = []
    async def answer(role, payload, schema):
        if role == 'supervisor':
            calls.append(payload)
            if len(calls) == 1:
                error = BankError('MODEL_RESPONSE_INVALID', 'synthetic schema failure', 502)
                error.diagnostic = {'kind':'schema_validation','fields':[{'type':'extra_forbidden','path':['<undeclared_field>']}]}
                raise error
            return schema.model_validate({'topic':'combined','amount_fen':50000})
        return schema.model_validate({'focus_claim_ids':[],'reads':[],'rationale':'核对证据'})
    app.state.research.model.astructured = answer
    async def independent_check(role, payload, schema):
        return schema.model_validate({"issues": []})
    app.state.graph.semantic_verifier.model.astructured = independent_check
    with TestClient(app) as client:
        headers = login(client)
        job = client.post('/api/research',headers=headers,json={'query':'深入研究理财，计划申购500元，只分析不办理'}).json()
        for _ in range(200):
            state = client.get('/api/research/'+job['id'],headers=headers).json()
            if state['status'] != 'RESEARCH_RUNNING':
                break
            time.sleep(.02)
        assert state['status'] == 'RESEARCH_READY', state
        assert len(calls) == 2 and calls[0]['query'] == calls[1]['query']
        assert state['report']['constraints']['analysis_only'] and state['report']['options'] == []
