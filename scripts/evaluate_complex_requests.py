"""Run the ten acceptance scenarios in isolated banks, optionally with live models.

Public directory responses are fixed official-format fixtures for reproducibility.
--live uses the configured model/Agents SDK/Chroma. No existing account is touched.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import shutil
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import traceback

from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
from bank_agent.app import create_app  # noqa: E402
from bank_agent.config import Settings  # noqa: E402
from bank_agent.storage import AgentRun  # noqa: E402
from conftest import login  # noqa: E402
from test_complex_requests import CASES, test_complex_request_acceptance  # noqa: E402


def check_paraphrase(client, headers, case):
    from conftest import payroll
    from test_public_information import ask, source
    from test_request_goal_repairs import ETF_DIRECTORY
    from test_complex_requests import advisory, transfers
    payroll(client,headers,450000 if case['id']=='P04' else 600000)
    source(client,funds=ETF_DIRECTORY)
    before=client.app.state.bank.facts('u_demo')['account']['balance_fen']
    result=ask(client,headers,case['message'])
    assert result['status'] in {'WAIT_CONFIRM','WAIT_MFA'},result
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==before
    if case['id']=='P02':
        assert [n['operation'] for n in result['nodes']]==['cards.apply'],result
        assert result['nodes'][0]['params']['kind']=='virtual'
        assert len(result['request_goals'])==2
        assert '1500' in json.dumps(result['request_goals'],ensure_ascii=False).replace(',','')
    else:
        amount={'P01':50000,'P03':300000,'P04':120000}[case['id']]
        assert transfers(result)==[('妈妈',amount)],result
        assert all(n['operation'] in {'funds.transfer','balance.query'} for n in result['nodes'])
        if case['id']!='P03':
            assert len(result['request_goals'])==3,result
            assert advisory(result)['query']['budget_fen']==(330000 if case['id']=='P04' else 100000)
            assert any(g['kind']=='capability_gap' and '会员' in g['title'] for g in result['request_goals'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--paraphrases',action='store_true',help='Run four additional natural-language variations')
    parser.add_argument('--output',default='docs/complex-requests-evaluation.json')
    parser.add_argument('--case-id',help='Run one specified case without changing its assertions')
    args = parser.parse_args()
    configured = Settings.from_env()
    if args.live and not configured.model_api_key:
        raise SystemExit('Model credentials are not configured')
    if args.paraphrases and not args.live:
        raise SystemExit('The paraphrase set requires --live to validate the business-model fallback')
    case_file='evaluation/cases/complex_paraphrases_4.json' if args.paraphrases else 'evaluation/cases/complex_requests_10.json'
    cases=json.loads((ROOT/case_file).read_text(encoding='utf-8')) if args.paraphrases else CASES
    if args.case_id:
        cases=[c for c in cases if c['id']==args.case_id]
        if not cases:
            raise SystemExit('Unknown case id')
    check=check_paraphrase if args.paraphrases else test_complex_request_acceptance
    report = {'at':datetime.now(timezone.utc).isoformat(),'mode':'live_configured_model' if args.live else 'deterministic_demo',
              'model':configured.model_name if args.live else None,'public_sources':'fixed_directory_fixture',
              'criteria':case_file,'cases':[]}
    for case in cases:
        with TemporaryDirectory(prefix='bank-complex-eval-') as directory:
            # Only immutable model weights are reused; accounts, memories and
            # Chroma indexes are always new for every scenario.
            cache = configured.data_dir/'embedding_cache'
            if args.live and configured.memory_embedding=='fastembed' and cache.exists():
                shutil.copytree(cache,Path(directory)/'embedding_cache')
            settings = replace(configured,data_dir=Path(directory),model_mode='llm' if args.live else 'demo',
                               memory_chroma_url='',memory_backend='chroma',scheduler_mode='external')
            app = create_app(settings)
            with TestClient(app) as client:
                headers = login(client)
                row = {'id':case['id'],'dimension':case['dimension']}
                started = perf_counter()
                try:
                    check(client,headers,case)
                    row['passed'] = True
                except Exception as error:
                    row.update(passed=False,error_type=type(error).__name__,failure=traceback.format_exc(limit=3)[-2500:])
                    frames=traceback.extract_tb(error.__traceback__)
                    row['failed_assertion']=[{'function':f.name,'line':f.lineno,'statement':f.line} for f in frames[-2:]]
                row['scenario_seconds'] = round(perf_counter()-started,3)
                with app.state.db.read() as session:
                    runs = list(session.scalars(select(AgentRun)))
                    row['model_calls'] = len(runs)
                    row['model_runs'] = [{k:getattr(r,k,None) for k in ('role','status','duration_ms','prompt_tokens','completion_tokens','total_tokens','error_code')} for r in runs]
                row['embedding_backend'] = app.state.graph.planner.memory.embeddings.backend
                report['cases'].append(row)
                print(json.dumps({k:row[k] for k in ('id','passed','scenario_seconds','model_calls')},ensure_ascii=False),flush=True)
        report['passed'] = sum(r['passed'] for r in report['cases'])
        output = ROOT/args.output
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed']==len(cases) else 1


if __name__=='__main__':
    raise SystemExit(main())
