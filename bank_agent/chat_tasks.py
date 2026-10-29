"""Lease-owned request workers with durable progress and explicit interruption.

Workers never replay a RUNNING request after a crash: an amendment may already
have committed. Existing plans and receipts are returned for inspection instead.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib

from pydantic import Field
from sqlalchemy import select

from .errors import BankError
from .memory import SENSITIVE
from .progress import progress_sink
from .runtime import append_event
from .runtime_context import TaskContext, task_state
from .schemas import ChatRequest, Intent
from .storage import ChatTask, Document, Plan, now_iso, save_document, uid

TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'}


class ChatTaskRequest(ChatRequest):
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class ChatTaskService:
    def __init__(self, db, intake, harness, settings):
        if settings.chat_worker_mode not in {'embedded','external','disabled'} or settings.chat_concurrency<1 or settings.chat_queue_limit<1 or settings.chat_lease_seconds<=0:
            raise BankError('TASK_CONFIG_INVALID','后台任务配置无效',503)
        self.db, self.intake, self.harness, self.settings = db, intake, harness, settings
        self.worker_id = uid('worker')
        self.tasks = {}
        self.slots = asyncio.Semaphore(max(1,settings.chat_concurrency))

    def snapshot(self, user_id, task_id):
        with self.db.read() as session:
            row = session.get(ChatTask, task_id)
            if not row or row.owner_id != user_id:
                raise BankError('TASK_NOT_FOUND', '这项任务不存在或无权访问', 404)
            return {key: getattr(row,key) for key in ('id','conversation_id','status','stage','label','revision',
                'cancel_requested','created_at','updated_at','first_usable_ms','partial','result','events')}

    async def submit(self, user_id, request):
        request_key = hashlib.sha256((user_id + ':' + request.client_request_id).encode()).hexdigest()
        with self.db.write() as session:
            row = session.scalar(select(ChatTask).where(ChatTask.request_key == request_key))
            safe_message = '[敏感凭证已拦截并移除]' if SENSITIVE.search(request.message) else request.message
            if row:
                if row.message != safe_message or (request.conversation_id and row.conversation_id != request.conversation_id):
                    raise BankError('TASK_KEY_CONFLICT','这次请求标识已经用于其他内容，请核对原任务',409)
                pinned = session.get(Document, 'chat_model_' + row.id)
                if request.model_id and pinned and pinned.data['model_id'] != request.model_id:
                    raise BankError('TASK_KEY_CONFLICT','本次请求已绑定原模型，请勿重复提交',409)
                task_id = row.id
            else:
                active = list(session.scalars(select(ChatTask).where(ChatTask.status.in_(['QUEUED','RUNNING']))))
                if len(active) >= self.settings.chat_queue_limit:
                    raise BankError('TASK_QUEUE_FULL','当前任务较多，请稍后再试',429)
                task_id = uid('chat_task')
                conversation_id = request.conversation_id or uid('conversation')
                if getattr(self.harness, 'workspace', None):
                    profile, choice = self.harness.workspace.ensure(session, user_id, conversation_id,
                        model_id=request.model_id, message=safe_message)
                    save_document(session, 'chat_model_' + task_id, 'chat_model', user_id,
                        {'model_id': profile.id, 'model_name': profile.model, 'context_tokens': profile.context_tokens,
                         'credential_version': profile.credential_version,
                         'model_explicit': choice.get('model_explicit', False)})
                session.add(ChatTask(id=task_id,owner_id=user_id,
                    conversation_id=conversation_id,request_key=request_key,
                    message=safe_message,created_at=now_iso(),updated_at=now_iso()))
        self._schedule(user_id, task_id)
        return self.snapshot(user_id,task_id)

    def _schedule(self, user_id, task_id):
        if self.settings.chat_worker_mode != 'embedded':
            return
        if task_id not in self.tasks or self.tasks[task_id].done():
            task = asyncio.create_task(self._run(user_id,task_id))
            self.tasks[task_id] = task
            task.add_done_callback(lambda done:self.tasks.pop(task_id,None) if self.tasks.get(task_id) is done else None)

    def _claim(self, user_id, task_id):
        with self.db.write() as session:
            row = session.get(ChatTask,task_id)
            if not row or row.owner_id != user_id or row.status != 'QUEUED' or row.cancel_requested:
                return None
            row.status, row.worker_id = 'RUNNING', self.worker_id
            row.lease_until = (datetime.now(timezone.utc)+timedelta(seconds=self.settings.chat_lease_seconds)).isoformat()
            row.updated_at, row.revision = now_iso(), row.revision+1
            model = session.get(Document, 'chat_model_' + task_id)
            return row.message,row.conversation_id,model.data if model and model.owner_id == user_id else {}

    def _publish(self, task_id, stage, label, partial=None):
        with self.db.write() as session:
            row = session.get(ChatTask,task_id)
            if not row or row.worker_id != self.worker_id:
                raise BankError('TASK_LEASE_LOST','任务执行权已变更，请先核对已有办理结果',409)
            if row.cancel_requested:
                raise asyncio.CancelledError()
            if row.status != 'RUNNING':
                raise BankError('TASK_LEASE_LOST','任务已经结束，请先核对已有办理结果',409)
            if row.lease_until <= now_iso():
                raise BankError('TASK_LEASE_LOST','任务执行权已失效，请先核对已有办理结果',409)
            if partial is not None:
                row.partial = partial
                if row.first_usable_ms is None:
                    first = (datetime.now(timezone.utc)-datetime.fromisoformat(row.created_at)).total_seconds()*1000
                    row.first_usable_ms = max(0,first)
                    append_event(session, 'task.first_usable', row.owner_id, task_id=task_id,
                                 details={'duration_ms':row.first_usable_ms})
            row.stage, row.label, row.updated_at = stage, label, now_iso()
            row.revision += 1
            row.events = [*row.events[-49:],{'stage':stage,'label':label,'at':row.updated_at}]

    async def _heartbeat(self, task_id):
        while True:
            await asyncio.sleep(max(.1,min(1,self.settings.chat_lease_seconds/3)))
            with self.db.read() as session:
                row = session.get(ChatTask,task_id)
                if not row or row.worker_id != self.worker_id or row.status != 'RUNNING':
                    return
                if row.cancel_requested or row.lease_until <= now_iso():
                    running = self.tasks.get(task_id)
                    if running:
                        running.cancel()
                    return
                renew = row.lease_until < (datetime.now(timezone.utc)+timedelta(seconds=self.settings.chat_lease_seconds/2)).isoformat()
            if renew:
                with self.db.write() as session:
                    row = session.get(ChatTask,task_id)
                    if row and row.worker_id == self.worker_id and row.status=='RUNNING' and row.lease_until>now_iso():
                        row.lease_until = (datetime.now(timezone.utc)+timedelta(seconds=self.settings.chat_lease_seconds)).isoformat()

    def _complete(self, task_id, status, result, label):
        with self.db.write() as session:
            row = session.get(ChatTask,task_id)
            if not row or row.worker_id != self.worker_id or row.status in TERMINAL:
                return
            if status=='SUCCEEDED' and row.lease_until <= now_iso():
                status,result,label='INTERRUPTED',row.partial,'任务执行权已失效，请核对已有办理结果'
            row.status,row.result,row.label = status,result,label
            row.updated_at,row.revision = now_iso(),row.revision+1
            duration = max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(row.created_at)).total_seconds()*1000)
            # Readers must observe the terminal state and its metrics together.
            append_event(session, 'task.finished', row.owner_id, task_id=task_id,
                         details={'duration_ms':duration,'status':status})

    async def _existing_plan(self, user_id, task_id):
        with self.db.read() as session:
            row = session.scalar(select(Plan).where(Plan.user_id==user_id,Plan.thread_id==task_id))
            plan_id = row.id if row else None
        return await asyncio.to_thread(self.intake.banking.plans.snapshot,user_id,plan_id) if plan_id else None

    async def _cancel_plan(self, user_id, task_id):
        plan = await self._existing_plan(user_id,task_id)
        if plan and plan['status'] not in {'COMPLETED','CANCELLED'}:
            try:
                plan = await self.intake.banking.amutate(user_id,plan['id'],'cancel')
            except BankError:
                plan = await self._existing_plan(user_id,task_id)
        if plan:
            from .storage import Document
            with self.db.write() as session:
                for key in ('request_goals_'+plan['id'],f"goal_agenda_{user_id}_{plan['conversation_id']}"):
                    doc = session.get(Document,key)
                    if not doc or doc.owner_id!=user_id or doc.data.get('plan_id')!=plan['id']:
                        continue
                    goals = [{**g,'status':'CANCELLED','revision':g.get('revision',1)+1,
                        'notice':'已停止这项目标的后续处理，没有因此办理资金操作。'}
                        if g['kind']!='bank_operation' and g['status'] not in {'ANSWERED','CANCELLED'} else g for g in doc.data['goals']]
                    doc.data = {**doc.data,'goals':goals,'revision':doc.data.get('revision',1)+1}
            plan = await asyncio.to_thread(self.intake.banking.plans.snapshot,user_id,plan['id'])
        return plan

    async def _run(self, user_id, task_id):
        heartbeat = None
        context = sink = None
        claimed = False
        try:
            async with self.slots:
                value = await asyncio.to_thread(self._claim,user_id,task_id)
                if not value:
                    return
                claimed = True
                message, conversation_id, model = value
                context = task_state.set(TaskContext(user_id,conversation_id,task_id,task_state.get().request_id,
                    model_id=model.get('model_id', ''), model_name=model.get('model_name', ''),
                    model_context_tokens=model.get('context_tokens'), model_explicit=model.get('model_explicit', False),
                    model_credential_version=model.get('credential_version', 0)))
                sink = progress_sink.set(lambda stage,label,partial=None:self._publish(task_id,stage,label,partial))
                heartbeat = asyncio.create_task(self._heartbeat(task_id))
                self._publish(task_id,'understanding','正在识别需求与核对上下文')
                async with self.intake.banking.process_locks.ahold(f'request:{user_id}:{conversation_id}'):
                    if message == '[敏感凭证已拦截并移除]':
                        result = await self.intake.banking.astart(user_id,message,conversation_id,intent=Intent(
                            summary='敏感凭证已拦截',reject_reason='请在安全中心输入凭证，不要发送到聊天',rejection_kind='security'))
                    else:
                        result = await self.intake.chat(user_id,message,conversation_id)
                    self._publish(task_id,'ready','已完成本次需求核查',result)
                    await asyncio.to_thread(self._complete,task_id,'SUCCEEDED',result,'本次处理已完成，办理状态以卡片为准')
        except asyncio.CancelledError:
            if claimed:
                progress_sink.set(None)  # Cleanup observes bank state, not the cancelled generation lease.
                with self.db.read() as session:
                    row = session.get(ChatTask,task_id)
                    requested = bool(row and row.cancel_requested)
                # A shutdown does not cancel the user's bank plan.
                result = await self._cancel_plan(user_id,task_id) if requested else await self._existing_plan(user_id,task_id)
                await asyncio.to_thread(self._complete,task_id,'CANCELLED' if requested else 'INTERRUPTED',result,
                    '已停止后续处理；已有提交结果请查看办理卡片' if requested else '服务已中断，请先核对已有办理结果')
        except Exception as error:
            result = {'error':{'code':error.code if isinstance(error,BankError) else 'TASK_FAILED',
                'message':error.message if isinstance(error,BankError) else '本次处理暂未完成，请先核对已有办理状态'}}
            await asyncio.to_thread(self._complete,task_id,'FAILED',result,'本次处理暂未完成')
        finally:
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if sink is not None:
                progress_sink.reset(sink)
            if context is not None:
                task_state.reset(context)

    async def cancel(self, user_id, task_id):
        self.snapshot(user_id,task_id)
        with self.db.write() as session:
            row = session.get(ChatTask,task_id)
            if row.status in TERMINAL:
                return self.snapshot(user_id,task_id)
            row.cancel_requested = True
            row.revision,row.updated_at = row.revision+1,now_iso()
            if row.status == 'QUEUED':
                row.status,row.label = 'CANCELLED','已取消尚未开始的任务'
        running = self.tasks.get(task_id)
        if running and not running.done():
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        return self.snapshot(user_id,task_id)

    async def maintain(self):
        while True:
            await self.recover()
            await asyncio.sleep(1)

    async def recover(self):
        pending = []
        with self.db.write() as session:
            for row in session.scalars(select(ChatTask).where(ChatTask.status.in_(['QUEUED','RUNNING']))):
                if row.status == 'QUEUED':
                    pending.append((row.owner_id,row.id))
                elif row.lease_until <= now_iso():
                    row.status,row.label = 'INTERRUPTED','上次处理已中断，请核对已有计划后继续'
                    row.updated_at,row.revision = now_iso(),row.revision+1
        for user_id, task_id in pending:
            self._schedule(user_id,task_id)

    async def shutdown(self):
        pending = [t for t in self.tasks.values() if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending,return_exceptions=True)
