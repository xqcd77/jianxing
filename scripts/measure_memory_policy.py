"""Offline curation/read-policy smoke measurement on a temporary database."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.storage import MemoryRecord


def measure():
    with TemporaryDirectory(prefix='jianxing-memory-policy-') as directory:
        app = create_app(Settings(data_dir=Path(directory), memory_backend='numpy', model_mode='demo'))
        with TestClient(app):
            memory = app.state.memory
            preference = memory.remember('u_demo', '不要动用生日预算500元', kind='preference',
                roles=['funds'], scope='user', source_type='user_preference')
            for _ in range(3):
                memory.remember('u_demo', '核查过的历史业务说明', roles=['funds'], conversation_id='local_sample')
            calls, encode = [], memory.embeddings.encode

            def count(texts, **kwargs):
                calls.append({'query': bool(kwargs.get('query')), 'count': len(texts)})
                return encode(texts, **kwargs)

            memory.embeddings.encode = count
            memory.search('u_demo', '查余额', 'funds', 'local_sample')
            semantic_calls = len(calls)
            calls.clear()
            result = memory.search_for_request('u_demo', '查余额', 'funds', 'local_sample')
            direct_calls = len(calls)
            with memory.db.read() as session:
                originals_before = session.scalar(select(func.count()).select_from(MemoryRecord))
            for _ in range(20):
                if not memory.curator.run_pending()['more']:
                    break
            status = memory.curator.status('u_demo')
            with memory.db.read() as session:
                originals_after = session.scalar(select(func.count()).select_from(MemoryRecord))
            return {'scope': 'isolated_synthetic_sample_not_model_evaluation',
                'semantic_embedding_calls': semantic_calls, 'balance_policy_embedding_calls': direct_calls,
                'read_policy': result['read_policy'],
                'required_preference_retained': preference in {r['id'] for r in result['items']},
                'originals_before': originals_before, 'originals_after': originals_after,
                'curation_status': status['status'], 'memory_states': status['counts'], 'provider_calls': 0}


if __name__ == '__main__':
    result = measure()
    target = Path(__file__).resolve().parents[1] / '.data/memory_batch3_measurements.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
