"""Measure real demo research handoffs locally, without a provider request.

python -m scripts.measure_stage_context
Numbers are conservative UTF-8 budget estimates, not billed model tokens.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from fastapi.testclient import TestClient

from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.context_builder import CONTEXT_POLICY
from bank_agent.deep_research import safe_derive
from bank_agent.errors import BankError
from bank_agent.research_schemas import ResearchBrief
from bank_agent.seed import DEMO_PASSWORD
from bank_agent.stage_context import StageContextPacker
from bank_agent.storage import canonical


def measure():
    with TemporaryDirectory(prefix='jianxing-stage-measure-') as directory:
        settings = Settings(data_dir=Path(directory), model_mode='demo', memory_backend='numpy',
            model_context_tokens=1_000_000, model_api_key='synthetic-no-network')
        with TestClient(create_app(settings)) as client:
            app = client.app
            auth = client.post('/api/auth/login', json={'username': 'demo', 'password': DEMO_PASSWORD})
            headers = {'Authorization': 'Bearer ' + auth.json()['access_token']}
            response = client.post('/api/demo/payroll', headers=headers,
                json={'event_id': 'measurement-only', 'amount_fen': 2_000_000})
            response.raise_for_status()
            response = client.post('/api/research', headers=headers, json={
                'query': '深入核查收支和产品条款，预留生活费3000元，只分析，不要买',
                'constraints': {'reserve_fen': 300000, 'analysis_only': True, 'forbid_purchase': True}})
            response.raise_for_status()
            research_id = response.json()['id']
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                data = client.get('/api/research/' + research_id, headers=headers).json()
                if data['status'] != 'RESEARCH_RUNNING':
                    break
                time.sleep(.02)
            if data['status'] != 'RESEARCH_READY':
                raise RuntimeError('demo research did not complete: ' + data['status'])
            service = app.state.research
            original = service._load('u_demo', research_id)
            report = original['report']
            evidence = {e['id']: e for e in report['evidence']}
            brief = ResearchBrief.model_validate(report['constraints'])
            results = {role: safe_derive(role, evidence, brief) for role in report['roles']}
            for result in results.values():
                result.update(focus_claim_ids=[], invalid_claim_ids=[], rejected_reads=[], model_error=None)
            state = {'id': research_id, 'user_id': 'u_demo', 'conversation_id': original['conversation_id'],
                'query': original['query'], 'brief': report['constraints'], 'baseline': original['baseline'],
                'round': report['rounds'], 'evidence': evidence, 'results': results,
                'verification': {'fresh': True, 'gaps': [], 'verified': deepcopy(results)},
                'role_returns': original['role_return_ids']}
            service.settings = replace(service.settings, model_mode='llm')
            service.model.settings = replace(service.model.settings, model_mode='llm')
            measurements = []

            async def capture(role, payload, schema):
                prepared = app.state.harness.prepare_payload(role, payload)
                model = service.model
                before = model._request(role, prepared, schema)
                if prepared.get('task_brief'):
                    before['messages'][0]['content'] += '\n' + CONTEXT_POLICY
                before = app.state.model_registry.adapt(before, role)
                after = model._contextual_request(role, prepared, schema)
                packed = json.loads(after['messages'][-1]['content'])
                assert StageContextPacker.expand(packed) == prepared
                budget = app.state.harness.budget
                original_request, _ = budget.prepare(before)
                packed_request, _ = budget.prepare(after)
                before_count, after_count = budget.estimate(original_request), budget.estimate(packed_request)
                measurements.append({'stage': 'research_synthesis', 'method': 'utf8_conservative_estimate',
                    'input_before': before_count, 'input_after': after_count,
                    'reduction_pct': round((1 - after_count / before_count) * 100, 2),
                    'json_bytes_before': len(canonical(prepared).encode('utf-8')),
                    'json_bytes_after': len(canonical(packed).encode('utf-8')), 'lossless_roundtrip': True,
                    'provider_calls': 0})
                # Stop before any transport; do not simulate a model-generated answer.
                raise BankError('MEASUREMENT_ONLY', 'local measurement, no provider call', 503)

            service.model.astructured = capture
            with app.state.harness.scope('u_demo', original['conversation_id'], 'local_measurement'):
                asyncio.run(service._synthesize(state))
            return {'scope': 'isolated_demo_research_not_paid_evaluation', 'sample_count': 1,
                    'includes': ['system_prompt', 'schema', 'role_returns', 'TaskBrief', 'source_evidence',
                                 'stage_reference_policy', 'budget_conflict_policy'],
                    'measurements': measurements[:1]}


if __name__ == '__main__':
    result = measure()
    target = Path(__file__).resolve().parents[1] / '.data/stage_protocol_measurements.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
