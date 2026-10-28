import asyncio
from datetime import datetime, timedelta, timezone
import time
import pytest

from bank_agent.storage import ChatTask, now_iso
from conftest import login, payroll
from test_public_information import source
from test_request_goal_repairs import ETF_DIRECTORY


def submit(client, headers, message='给妈妈转500元，1000元看看ETF', key='acceptance-request-001'):
    response = client.post('/api/chat/tasks',headers=headers,json={'message':message,'client_request_id':key})
    assert response.status_code == 202,response.text
    return response.json()


def wait_for(client, headers, task_id, predicate, timeout=8):
    until = time.monotonic()+timeout
    while time.monotonic()<until:
        value = client.get('/api/chat/tasks/'+task_id,headers=headers).json()
        if predicate(value):
            return value
        time.sleep(.04)
    raise AssertionError(value)


def slow_information(client, monkeypatch):
    source(client,funds=ETF_DIRECTORY)
    original = client.app.state.graph.information.answer
    async def delayed(query):
        await asyncio.sleep(1.5)
        return await original(query)
    monkeypatch.setattr(client.app.state.graph.information,'answer',delayed)


def test_partial_progress_arrives_before_information_and_has_first_usable_metric(client,headers,monkeypatch):
    payroll(client,headers)
    slow_information(client,monkeypatch)
    task = submit(client,headers)
    partial = wait_for(client,headers,task['id'],lambda t:t['partial'] is not None)
    assert partial['status']=='RUNNING'
    assert partial['partial']['nodes'][0]['status']=='WAIT_CONFIRM'
    assert len(partial['partial']['request_goals'])==2
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen']==600000
    finished = wait_for(client,headers,task['id'],lambda t:t['status']=='SUCCEEDED')
    assert finished['result']['request_goals'][1]['result']['candidates']
    stream = client.get('/api/chat/tasks/'+task['id']+'/events',headers=headers)
    assert stream.headers['content-type'].startswith('application/x-ndjson') and 'progress' in stream.text
    metrics = client.get('/api/runtime/metrics',headers=headers).json()['chat_tasks']
    assert metrics['first_usable']['count']==1 and metrics['first_usable']['avg_ms']<metrics['total']['avg_ms']


def test_cancel_stops_remaining_read_and_cancels_only_unsubmitted_plan(client,headers,monkeypatch):
    payroll(client,headers)
    slow_information(client,monkeypatch)
    task = submit(client,headers)
    partial = wait_for(client,headers,task['id'],lambda t:t['partial'] is not None)
    reply = client.post('/api/chat/tasks/'+task['id']+'/cancel',headers=headers,json={})
    assert reply.status_code==200,reply.text
    assert reply.json()['status']=='CANCELLED'
    plan = client.get('/api/plans/'+partial['partial']['id'],headers=headers).json()
    assert plan['nodes'][0]['status']=='CANCELLED'
    assert plan['request_goals'][1]['status']=='CANCELLED'
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen']==600000
    assert client.post('/api/chat/tasks/'+task['id']+'/cancel',headers=headers,json={}).json()['status']=='CANCELLED'


def test_client_retry_is_idempotent_and_task_access_is_owned(client,headers):
    payroll(client,headers)
    task = submit(client,headers,'给妈妈转500元')
    done = wait_for(client,headers,task['id'],lambda t:t['status']=='SUCCEEDED')
    retry = submit(client,headers,'给妈妈转500元')
    assert retry['id']==task['id']
    assert len(client.get('/api/plans',headers=headers).json())==1
    other = login(client,'wang')
    assert client.get('/api/chat/tasks/'+task['id'],headers=other).status_code==404
    assert client.post('/api/chat/tasks/'+task['id']+'/cancel',headers=other,json={}).status_code==404
    assert done['result']['nodes'][0]['status']=='WAIT_CONFIRM'


def test_expired_worker_is_interrupted_without_replaying_and_queued_work_recovers(client,headers):
    service = client.app.state.chat_tasks
    with service.db.write() as session:
        session.add(ChatTask(id='chat_task_expired',owner_id='u_demo',conversation_id='recovery-one',request_key='recovery-one',
            message='给妈妈转500元',status='RUNNING',worker_id='dead-worker',created_at=now_iso(),updated_at=now_iso(),
            lease_until=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))
        session.add(ChatTask(id='chat_task_queued',owner_id='u_demo',conversation_id='recovery-two',request_key='recovery-two',
            message='查余额',status='QUEUED',created_at=now_iso(),updated_at=now_iso()))
    client.portal.call(service.recover)
    assert service.snapshot('u_demo','chat_task_expired')['status']=='INTERRUPTED'
    wait_for(client,headers,'chat_task_queued',lambda t:t['status']=='SUCCEEDED')
    assert all(n['operation']=='balance.query' for p in client.get('/api/plans',headers=headers).json() for n in p['nodes'])


def test_sensitive_input_never_persists_in_background_request(client,headers):
    task = submit(client,headers,'验证码123456',key='sensitive-request-001')
    done = wait_for(client,headers,task['id'],lambda t:t['status']=='SUCCEEDED')
    assert done['result']['status']=='REJECTED'
    with client.app.state.db.read() as session:
        row = session.get(ChatTask,task['id'])
        assert '123456' not in row.message and '123456' not in str(row.result)


def test_cancel_does_not_reverse_a_transfer_submitted_during_public_read(client,headers,monkeypatch):
    from conftest import finish
    payroll(client,headers)
    slow_information(client,monkeypatch)
    task=submit(client,headers)
    partial=wait_for(client,headers,task['id'],lambda t:t['partial'] is not None)
    completed=finish(client,headers,partial['partial'])
    assert completed['status']=='COMPLETED'
    reply=client.post('/api/chat/tasks/'+task['id']+'/cancel',headers=headers,json={}).json()
    assert reply['status']=='CANCELLED' and reply['result']['nodes'][0]['status']=='SUCCEEDED'
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen']==550000


def test_standalone_workers_claim_external_queue_once(tmp_path):
    import os
    import subprocess
    import sys
    from fastapi.testclient import TestClient
    from bank_agent.app import create_app
    from bank_agent.config import Settings
    from conftest import login
    settings=Settings(data_dir=tmp_path,memory_backend='numpy',chat_worker_mode='external',scheduler_mode='disabled')
    app=create_app(settings)
    processes=[]
    with TestClient(app) as client:
        headers=login(client)
        task=submit(client,headers,'查余额',key='external-worker-request-001')
        assert task['status']=='QUEUED'
        env={**os.environ,'BANK_DATA_DIR':str(tmp_path),'BANK_MODEL_MODE':'demo','BANK_MODEL_API_KEY':'',
             'BANK_MEMORY_EMBEDDING':'lexical','BANK_MEMORY_BACKEND':'numpy','BANK_SCHEDULER_MODE':'disabled',
             'BANK_CHAT_WORKER_MODE':'embedded'}
        try:
            for _ in range(2):
                processes.append(subprocess.Popen([sys.executable,'-m','bank_agent.worker'],env=env,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
            done=wait_for(client,headers,task['id'],lambda t:t['status']=='SUCCEEDED',timeout=25)
            assert done['result']['nodes'][0]['operation']=='balance.query'
            assert len(client.get('/api/plans',headers=headers).json())==1
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=15)


@pytest.mark.parametrize('cancel',[False,True])
def test_late_information_result_cannot_overwrite_a_newer_budget_or_cancellation(client,headers,monkeypatch,cancel):
    import threading
    payroll(client,headers)
    source(client,funds=ETF_DIRECTORY)
    original=client.app.state.graph.information.answer
    entered,release=threading.Event(),threading.Event()
    async def delayed(query):
        if query.budget_fen==100000:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
        return await original(query)
    monkeypatch.setattr(client.app.state.graph.information,'answer',delayed)
    task=submit(client,headers,key='concurrent-goal-request-001')
    try:
        partial=wait_for(client,headers,task['id'],lambda t:t['partial'] is not None)
        assert entered.wait(2)
        path='/api/conversations/'+task['conversation_id']+'/goals'
        agenda=client.get(path,headers=headers).json()
        patch={'action':'cancel'} if cancel else {'fields':{'budget_fen':80000}}
        changed=client.post(path,headers=headers,json={'target_id':agenda['goals'][1]['id'],
            'expected_revision':agenda['revision'],**patch})
        assert changed.status_code==200,changed.text
        release.set()
        done=wait_for(client,headers,task['id'],lambda t:t['status']=='SUCCEEDED')
        if cancel:
            assert done['result']['request_goals'][1]['status']=='CANCELLED'
        else:
            assert done['result']['request_goals'][1]['query']['budget_fen']==80000
            assert done['result']['request_goals'][1]['result']['investment_budget']['budget_fen']==80000
        assert done['result']['id']==partial['partial']['id']
    finally:
        release.set()
