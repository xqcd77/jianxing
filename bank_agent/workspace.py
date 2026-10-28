"""Owner-scoped chat history and transparent model/context controls.

ChatTask is the durable transcript source; metadata never replaces the banking
agenda, approval, ledger or original evidence. Opening history is read-only.
"""
import hashlib

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, ValidationError
from sqlalchemy import select

from .errors import BankError
from .schemas import StrictModel
from .storage import AgentRun, ChatTask, Document, Plan, RuntimeEvent, now_iso, save_document, uid


def metadata_key(owner, conversation):
    return 'conversation_' + hashlib.sha256((owner + ':' + conversation).encode()).hexdigest()


class ConversationCreate(StrictModel):
    model_id: str | None = Field(default=None, max_length=40)


class ConversationUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    archived: bool | None = None
    model_id: str | None = Field(default=None, max_length=40)


class ConversationService:
    def __init__(self, db, registry, builder, memory):
        self.db, self.registry, self.builder, self.memory = db, registry, builder, memory

    def metadata(self, session, owner, conversation):
        row = session.get(Document, metadata_key(owner, conversation))
        return row if row and row.owner_id == owner and row.kind == 'conversation' else None

    def ensure(self, session, owner, conversation, *, model_id=None, message=''):
        row = self.metadata(session, owner, conversation)
        profile = self.registry.get(model_id or (row.data.get('model_id') if row else None),
                                    require_enabled=bool(model_id), owner_id=owner, session=session)
        data = row.data if row else {'conversation_id': conversation, 'title': '新会话',
                                    'archived': False, 'created_at': now_iso()}
        if message and data['title'] == '新会话':
            data = {**data, 'title': message.replace('\n', ' ')[:54]}
        value = {**data, 'model_id': profile.id, 'model_explicit': bool(model_id) or data.get('model_explicit', False),
                 'updated_at': now_iso()}
        save_document(session, metadata_key(owner, conversation), 'conversation', owner, value)
        return profile, value

    def _exists(self, session, owner, conversation):
        return (self.metadata(session, owner, conversation) is not None
                or session.scalar(select(ChatTask.id).where(ChatTask.owner_id == owner,
                    ChatTask.conversation_id == conversation).limit(1)) is not None
                or session.scalar(select(Plan.id).where(Plan.user_id == owner,
                    Plan.conversation_id == conversation).limit(1)) is not None
                or ((row := session.get(Document, f'dialogue_{owner}_{conversation}')) is not None
                    and row.owner_id == owner and row.kind == 'dialogue'))

    def require(self, owner, conversation):
        with self.db.read() as session:
            if not self._exists(session, owner, conversation):
                raise BankError('CONVERSATION_NOT_FOUND', '会话不存在或无权查看', 404)

    def create(self, owner, model_id=None):
        conversation = uid('conversation')
        with self.db.write() as session:
            _, value = self.ensure(session, owner, conversation, model_id=model_id)
        return value

    def update(self, owner, conversation, patch):
        self.require(owner, conversation)
        with self.db.write() as session:
            _, value = self.ensure(session, owner, conversation, model_id=patch.model_id)
            value = {**value, **patch.model_dump(exclude_none=True)}
            if patch.title is not None:
                title = patch.title.strip()
                if not title:
                    raise BankError('TITLE_EMPTY', '会话名称不能为空', 422)
                from .memory import SENSITIVE
                if SENSITIVE.search(title):
                    raise BankError('SENSITIVE_TITLE', '会话名称不能包含凭证', 422)
                value['title'] = title
            save_document(session, metadata_key(owner, conversation), 'conversation', owner, value)
        return value

    def listing(self, owner, query='', archived=False):
        with self.db.read() as session:
            items = {r.data['conversation_id']: dict(r.data) for r in session.scalars(select(Document).where(
                Document.owner_id == owner, Document.kind == 'conversation'))}
            # Backfill existing UI requests without rewriting or replaying them.
            rows = session.scalars(select(ChatTask).where(ChatTask.owner_id == owner)
                                   .order_by(ChatTask.created_at.desc(), ChatTask.id.desc()))
            seen = set()
            for row in rows:
                item = items.setdefault(row.conversation_id, {'conversation_id': row.conversation_id,
                    'title': row.message[:54], 'archived': False, 'created_at': row.created_at,
                    'updated_at': row.updated_at, 'model_id': self.registry.default_id})
                if row.conversation_id not in seen:
                    item['preview'], item['status'] = row.message[:100], row.status
                    item['updated_at'] = max(item['updated_at'], row.updated_at)
                    seen.add(row.conversation_id)
                if row.status in {'QUEUED', 'RUNNING'}:
                    item['active_task_id'] = row.id
                item['created_at'] = min(item['created_at'], row.created_at)
            for row in session.scalars(select(Plan).where(Plan.user_id == owner).order_by(Plan.created_at.desc())):
                if row.conversation_id:
                    items.setdefault(row.conversation_id, {'conversation_id': row.conversation_id,
                        'title': row.summary[:54], 'archived': False, 'created_at': row.created_at,
                        'updated_at': row.created_at, 'model_id': self.registry.default_id})
        matches = [v for v in items.values() if bool(v.get('archived')) == archived
                   and (not query or query.casefold() in (v['title'] + v.get('preview', '')).casefold())]
        return sorted(matches, key=lambda v: v['updated_at'], reverse=True)[:100]

    def detail(self, owner, conversation, *, before=None, limit=40):
        self.require(owner, conversation)
        with self.db.read() as session:
            meta = self.metadata(session, owner, conversation)
            value = dict(meta.data) if meta else {'conversation_id': conversation, 'title': '历史会话',
                                                 'model_id': self.registry.default_id}
            query = select(ChatTask).where(ChatTask.owner_id == owner, ChatTask.conversation_id == conversation)
            if before:
                cursor = session.get(ChatTask, before)
                if not cursor or cursor.owner_id != owner or cursor.conversation_id != conversation:
                    raise BankError('CURSOR_INVALID', '历史记录位置无效', 422)
                query = query.where((ChatTask.created_at < cursor.created_at)
                    | ((ChatTask.created_at == cursor.created_at) & (ChatTask.id < cursor.id)))
            rows = list(session.scalars(query.order_by(ChatTask.created_at.desc(), ChatTask.id.desc()).limit(limit + 1)))
            has_more = len(rows) > limit
            rows = rows[:limit]
            turns = []
            research_ids = []
            for row in reversed(rows):
                profile = session.get(Document, 'chat_model_' + row.id)
                model = profile.data if profile and profile.owner_id == owner else {}
                result = row.result or row.partial or {}
                answer = (result.get('error') or {}).get('message') or result.get('assistant_message') or result.get('message')
                studies = ([result] if result.get('status', '').startswith('RESEARCH_') else
                    [g.get('result') or {} for g in result.get('request_goals', []) if g.get('kind') == 'research'])
                for study in studies:
                    study_id = study.get('research_id') or study.get('id')
                    report = session.get(Document, study_id) if study_id else None
                    if (not report or report.owner_id != owner or report.kind != 'research'
                            or report.data.get('conversation_id') != conversation):
                        continue
                    research_ids.append(report.id)
                    summary = (report.data.get('report') or {}).get('summary')
                    if report.data.get('status') == 'RESEARCH_READY' and summary:
                        answer = summary if result.get('status', '').startswith('RESEARCH_') else '\n'.join(filter(None, [answer, summary]))
                turns.append({'id': row.id, 'message': row.message, 'answer': answer or row.label,
                    'status': row.status, 'created_at': row.created_at, 'model': model.get('model_name'),
                    'model_id': model.get('model_id', self.registry.default_id)})
            active = session.scalar(select(ChatTask).where(ChatTask.owner_id == owner,
                ChatTask.conversation_id == conversation, ChatTask.status.in_(['QUEUED', 'RUNNING']))
                .order_by(ChatTask.created_at.desc()).limit(1))
            if not rows and not before:
                legacy = session.get(Document, f'dialogue_{owner}_{conversation}')
                if legacy and legacy.owner_id == owner:
                    value['legacy_turns'] = legacy.data.get('turns', [])
            value.update(turns=turns, active_task_id=active.id if active else None,
                         next_before=rows[-1].id if has_more else None)
        context = self.builder.read(owner, '', conversation)
        value['plan_id'] = context.get('active_plan', {}).get('id')
        value['research_id'] = context.get('active_research_id') or (research_ids[-1] if research_ids else None)
        return value

    def context(self, owner, conversation):
        self.require(owner, conversation)
        with self.db.read() as session:
            meta = self.metadata(session, owner, conversation)
            profile = self.registry.get(meta.data.get('model_id') if meta else None, require_enabled=False,
                                        owner_id=owner, session=session)
            pair = session.execute(select(RuntimeEvent, AgentRun).join(AgentRun, RuntimeEvent.run_id == AgentRun.id)
                .where(RuntimeEvent.owner_id == owner, RuntimeEvent.conversation_id == conversation,
                    RuntimeEvent.kind == 'budget.checked', AgentRun.owner_id == owner, AgentRun.model_name == profile.model)
                .order_by(RuntimeEvent.seq.desc()).limit(1)).first()
        packet = {'model': profile.public(), 'measurement': 'not_available', 'input_tokens': None,
                  'estimated_input_tokens': None, 'context_limit': profile.context_tokens,
                  'output_reserved': 0, 'safety_margin': self.registry.settings.model_token_safety_margin,
                  'remaining_tokens': None, 'utilization_pct': None, 'measured_at': None,
                  'budget_remaining_tokens': None,
                  'removed_items': 0, 'removed_turns': 0}
        if pair:
            event, run = pair
            budget = event.details
            input_tokens = run.prompt_tokens if run.prompt_tokens is not None else budget['estimated_input_tokens']
            limit = min(profile.context_tokens, budget['context_limit'])
            reserved, margin = budget['output_reserved'], budget['safety_margin']
            packet.update(measurement='provider_usage' if run.prompt_tokens is not None else 'conservative_estimate',
                input_tokens=input_tokens, estimated_input_tokens=budget['estimated_input_tokens'], context_limit=limit,
                output_reserved=reserved, safety_margin=margin, remaining_tokens=max(0, limit-input_tokens-reserved-margin),
                budget_remaining_tokens=max(0, limit-budget['estimated_input_tokens']-reserved-margin),
                utilization_pct=round(min(100, 100 * input_tokens / limit), 2), measured_at=event.created_at,
                removed_items=budget.get('removed_items', 0), removed_turns=budget.get('removed_turns', 0))
        brief = self.builder.brief(owner, conversation).model_dump(mode='json')
        packet['task_summary'] = {'total': len(brief['goals']), 'closed': len(brief['closed_goal_ids']),
            'goals': [{'title': g['title'], 'status': g['status']} for g in brief['goals']],
            'negative_constraints': brief['negative_constraints']}
        packet['memories'] = [m for m in self.memory.list(owner)
            if m.get('conversation_id') in {None, '', conversation} and m['status'] == 'active']
        return packet


def workspace_router(service, current_user):
    router = APIRouter(prefix='/api')

    @router.get('/models')
    def models(user=Depends(current_user)):
        return service.registry.catalog(user['id'])

    @router.get('/models/{model_id}/config')
    def model_configuration(model_id: str, user=Depends(current_user)):
        return service.registry.configuration(user['id'], model_id)

    async def parse_private_request(request, schema):
        # FastAPI's default body validation can echo the original input. This
        # endpoint must never return a submitted credential, even on errors.
        try:
            return schema.model_validate(await request.json())
        except (ValueError, ValidationError):
            raise BankError('MODEL_CONFIG_INVALID', '请核对API Key和模型ID后重试', 422) from None

    @router.post('/models/{model_id}/config')
    async def configure_model(model_id: str, request: Request, user=Depends(current_user)):
        from .model_registry import ProviderConfiguration
        import asyncio
        payload = await parse_private_request(request, ProviderConfiguration)
        return await asyncio.to_thread(service.registry.configure, user['id'], model_id, payload)

    @router.post('/models/{model_id}/options')
    async def available_models(model_id: str, request: Request, user=Depends(current_user)):
        from .model_registry import ProviderDiscovery
        payload = await parse_private_request(request, ProviderDiscovery)
        return await service.registry.discover(user['id'], model_id, payload)

    @router.get('/conversations')
    def conversations(q: str = Query(default='', max_length=100), archived: bool = False, user=Depends(current_user)):
        return {'items': service.listing(user['id'], q, archived)}

    @router.post('/conversations', status_code=201)
    def create(request: ConversationCreate, user=Depends(current_user)):
        return service.create(user['id'], request.model_id)

    @router.get('/conversations/{conversation_id}')
    def detail(conversation_id: str, before: str | None = None, limit: int = Query(default=40, ge=1, le=100),
               user=Depends(current_user)):
        return service.detail(user['id'], conversation_id, before=before, limit=limit)

    @router.post('/conversations/{conversation_id}')
    def update(conversation_id: str, request: ConversationUpdate, user=Depends(current_user)):
        return service.update(user['id'], conversation_id, request)

    @router.get('/conversations/{conversation_id}/context')
    def context(conversation_id: str, user=Depends(current_user)):
        return service.context(user['id'], conversation_id)

    return router
