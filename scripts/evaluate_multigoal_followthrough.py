"""Live acceptance on isolated accounts; no existing account is used."""
from dataclasses import replace
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from bank_agent.app import create_app  # noqa: E402
from bank_agent.config import Settings  # noqa: E402
from bank_agent.storage import AgentRun  # noqa: E402
from conftest import login  # noqa: E402
from test_multigoal_followthrough import (  # noqa: E402
    test_two_transfers_plus_fractional_wealth_keep_all_goals,
    test_transfers_and_independent_research_have_separate_progress)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case-id')
    args=parser.parse_args()
    configured=Settings.from_env()
    if not configured.model_api_key:
        raise SystemExit('Model credentials are not configured')
    cases=[
        ('M01','原始多转账与理财续接','给妈妈转400元  给老王转1500元  剩下的钱的20%用于理财  并给出推荐',False),
        ('M02','中文金额与百分比','先向我母亲汇款四百元，接着给王同学转一千五百元，剩余资金的百分之二十用于理财，并帮我推荐。',False),
        ('M03','名词列举与分数预算','先处理两笔转账：妈妈400元、小王1500元；剩余可用资金的五分之一用于理财，帮我推荐。',False),
        ('M04','办理与独立研究','给妈妈转400元，给小王转1500元，研究本月收支与现金流',True),
    ]
    if args.case_id:
        cases=[case for case in cases if case[0]==args.case_id]
    report={'at':datetime.now(timezone.utc).isoformat(),'model':configured.model_name,
            'mode':'live_configured_model','isolated_accounts':True,'cases':[]}
    for key,dimension,message,research in cases:
        with TemporaryDirectory(prefix='bank-multigoal-live-') as directory:
            weights=configured.data_dir/'embedding_cache'
            if weights.exists():
                shutil.copytree(weights,Path(directory)/'embedding_cache')
            settings=replace(configured,data_dir=Path(directory),model_mode='llm',scheduler_mode='external',memory_chroma_url='')
            app=create_app(settings)
            with TestClient(app) as client:
                headers=login(client)
                row={'id':key,'dimension':dimension,'input':message}
                started=perf_counter()
                try:
                    if research:
                        test_transfers_and_independent_research_have_separate_progress(client,headers,message)
                    else:
                        test_two_transfers_plus_fractional_wealth_keep_all_goals(client,headers,message)
                    row['passed']=True
                except Exception as error:
                    row.update(passed=False,error_type=type(error).__name__,failure=traceback.format_exc(limit=4)[-3500:])
                row['duration_seconds']=round(perf_counter()-started,3)
                with app.state.db.read() as session:
                    row['model_runs']=[{k:getattr(r,k,None) for k in ('role','status','duration_ms','total_tokens','error_code')}
                        for r in session.scalars(select(AgentRun))]
                report['cases'].append(row)
                print(json.dumps({k:row[k] for k in ('id','passed','duration_seconds')},ensure_ascii=True),flush=True)
        report['passed']=sum(c['passed'] for c in report['cases'])
        (ROOT/'docs/multigoal-live-evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if all(c['passed'] for c in report['cases']) else 1


if __name__=='__main__':
    raise SystemExit(main())
