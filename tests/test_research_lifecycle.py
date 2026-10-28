import asyncio
import threading
import time
from fastapi.testclient import TestClient
from bank_agent.app import create_app
from bank_agent.config import Settings
from conftest import login


def slow_app(tmp_path, **kwargs):
    app = create_app(Settings(data_dir=tmp_path, model_mode='llm', model_api_key='synthetic-test-key',
        memory_embedding='lexical', scheduler_mode='disabled', **kwargs))
    entered = threading.Event()
    async def hold(*args):
        entered.set()
        await asyncio.sleep(30)
    app.state.research.model.astructured = hold
    return app, entered


def test_research_can_be_cancelled_by_owner_and_has_backpressure(tmp_path):
    app, entered = slow_app(tmp_path, research_queue_limit=1)
    with TestClient(app) as client:
        headers = login(client)
        job = client.post('/api/research', headers=headers, json={'query': '深入研究账单，只分析不办理'}).json()
        assert entered.wait(3)
        assert client.post('/api/research', headers=headers, json={'query': '再次深入研究账单'}).status_code == 429
        other = login(client, 'wang')
        assert client.post(f"/api/research/{job['id']}/cancel", headers=other).status_code == 404
        done = client.post(f"/api/research/{job['id']}/cancel", headers=headers).json()
        assert done['status'] == 'RESEARCH_CANCELLED'


def test_research_total_deadline_cancels_model_wait(tmp_path):
    app, _ = slow_app(tmp_path, research_timeout=.1)
    with TestClient(app) as client:
        headers = login(client)
        job = client.post('/api/research', headers=headers, json={'query': '深入研究账单'}).json()
        for _ in range(80):
            state = client.get('/api/research/' + job['id'], headers=headers).json()
            if state['status'] != 'RESEARCH_RUNNING':
                break
            time.sleep(.02)
        assert state['status'] == 'RESEARCH_BLOCKED' and '超' in state['message']


def test_restart_recovers_pending_read_only_research_from_checkpoint(tmp_path):
    app, entered = slow_app(tmp_path)
    with TestClient(app) as client:
        headers = login(client)
        job = client.post('/api/research', headers=headers, json={'query': '深入研究账单，只分析不办理'}).json()
        assert entered.wait(3)
    restarted = create_app(Settings(data_dir=tmp_path, model_mode='demo', memory_embedding='lexical', scheduler_mode='disabled'))
    with TestClient(restarted) as client:
        headers = login(client)
        for _ in range(150):
            state = client.get('/api/research/' + job['id'], headers=headers).json()
            if state['status'] != 'RESEARCH_RUNNING':
                break
            time.sleep(.02)
        assert state['status'] == 'RESEARCH_READY', state
        assert client.get('/api/overview', headers=headers).json()['account']['balance_fen'] == 0


def test_other_worker_sees_queued_job_and_can_cancel_owner_task(tmp_path):
    from datetime import datetime, timedelta, timezone
    from bank_agent.storage import Document
    owner, entered = slow_app(tmp_path, research_concurrency=1)
    observer, _ = slow_app(tmp_path, research_concurrency=1)
    with TestClient(owner) as first, TestClient(observer) as second:
        a, b = login(first), login(second)
        running = first.post('/api/research', headers=a, json={'query': '深入研究账单，只分析不办理'}).json()
        assert entered.wait(3)
        queued = first.post('/api/research', headers=a, json={'query': '再次深入研究账单，只分析不办理'}).json()
        for _ in range(60):
            if observer.state.graph.process_locks.busy('researchjob:' + queued['id']):
                break
            time.sleep(.02)
        with owner.state.db.write() as session:
            doc = session.get(Document, queued['id'])
            doc.data = {**doc.data, 'created_at': (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()}
        assert second.get('/api/research/' + queued['id'], headers=b).json()['status'] == 'RESEARCH_RUNNING'
        # A second worker recovering the same jobs cannot take ownership or change their state.
        second.portal.call(observer.state.research.recover)
        time.sleep(.1)
        assert second.get('/api/research/' + running['id'], headers=b).json()['status'] == 'RESEARCH_RUNNING'
        for job in (running, queued):
            result = second.post('/api/research/' + job['id'] + '/cancel', headers=b).json()
            assert result['status'] == 'RESEARCH_CANCELLED'
        for _ in range(80):
            if not owner.state.research.tasks:
                break
            time.sleep(.025)
        assert not owner.state.research.tasks
        assert first.get('/api/research/' + running['id'], headers=a).json()['status'] == 'RESEARCH_CANCELLED'
