from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import asyncio
import hashlib
import json
import logging

from fastapi import Depends, FastAPI, Header, Request, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import select

from .agents import ROLES, SCENARIOS
from .deep_banking import DeepPlannerAgent as PlannerAgent, DeepBankingGraph as BankingGraph
from .businesses import BusinessRequest, BusinessService
from .information import information_intent
from .config import Settings
from .harness import BankAgentHarness
from .runtime_middleware import RuntimeMiddleware
from .contracts import BusinessAction, BusinessCategory, PublicUser
from .engine import PlanService
from .plan_routes import plan_router
from .migrations import migrate_failure_policy, migrate_business_evidence, migrate_support_boundaries
from .errors import BankError
from .events import EventService
from .operations import BankService
from .sandbox import run_statistics
from .schemas import (
    ChatRequest,
    ClockRequest,
    Intent,
    LoginRequest,
    PayrollRequest,
    StrictModel,
    MemorySearch,
    PreferenceRequest,
)
from .security import AuthService
from .deep_research import DeepResearchService as ResearchService
from .research_schemas import ResearchRequest, ResearchSelection
from .research_tools import ensure_disclosures
from .seed import seed_database
from .storage import Database, Node, Plan, audit as record_audit, canonical, documents


class OTPRequest(StrictModel):
    otp: str = Field(min_length=6, max_length=6)
    demo_challenge_id: str | None = Field(default=None, min_length=1, max_length=80)


class PasswordRequest(StrictModel):
    old_password: str
    new_password: str
    otp: str
    demo_challenge_id: str | None = Field(default=None, min_length=1, max_length=80)


class DemoCodeRequest(StrictModel):
    purpose: Literal["unlock", "approve", "password"]
    plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    node_id: str | None = Field(default=None, min_length=1, max_length=150)
    fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class IncomingDecision(StrictModel):
    decision: Literal["confirm", "cancel"]


class AnalysisRequest(StrictModel):
    code: str = Field(min_length=1, max_length=12000)
    period: str = "2026-10"


def create_app(settings=None):
    settings = settings or Settings.from_env()
    db = Database(settings.data_dir)
    auth = AuthService(db, settings.data_dir, settings)
    seed_database(db, auth)
    migrate_failure_policy(db)
    migrate_business_evidence(db)
    migrate_support_boundaries(db)
    from .migrations import migrate_goal_state
    migrate_goal_state(db)
    ensure_disclosures(db)
    bank = BankService(db)
    plans = PlanService(db, bank, auth)
    if settings.orchestration_mode not in {"supervisor", "legacy"}:
        raise BankError("ORCHESTRATION_CONFIG_INVALID", "编排模式配置无效")
    supervisor = None
    if settings.orchestration_mode == "supervisor":
        from .supervisor import Supervisor
        from .supervisor_agents import BusinessHandlingAgent, IndependentVerifier
        from .supervisor_banking import SupervisorBankingGraph
        from .supervisor_research import SupervisorResearchService

        planner = BusinessHandlingAgent(db, bank, settings)
        graph = SupervisorBankingGraph(settings.data_dir, planner, plans, bank)
        graph.semantic_verifier = IndependentVerifier(settings)
        research = SupervisorResearchService(db, bank, graph, settings)
        supervisor = Supervisor(graph, research)
    else:
        planner = PlannerAgent(db, bank, settings)
        graph = BankingGraph(settings.data_dir, planner, plans, bank)
        research = ResearchService(db, bank, graph, settings)
    harness = BankAgentHarness(settings, db)
    harness.context_builder = planner.context_builder
    if supervisor is not None:
        from .role_returns import RoleReturnStore
        from .stage_context import StageContextPacker
        harness.role_returns = RoleReturnStore(db)
        harness.stage_packer = StageContextPacker()
    from .model_registry import ModelRegistry
    from .workspace import ConversationService, workspace_router
    registry = ModelRegistry(settings, db, auth.fernet)
    harness.model_registry = registry
    workspace = ConversationService(db, registry, planner.context_builder, planner.memory)
    harness.workspace = workspace
    planner.model.harness = research.model.harness = harness
    if supervisor is not None:
        graph.semantic_verifier.model.harness = harness
    events = EventService(db, bank, graph, settings)
    businesses = BusinessService(db, bank, plans, settings)
    from .intake import RequestIntake
    intake = RequestIntake(graph, research, supervisor, events)
    if supervisor is not None:
        supervisor.intake = intake
    from .chat_tasks import ChatTaskService, ChatTaskRequest
    chat_tasks = ChatTaskService(db,intake,harness,settings)

    @asynccontextmanager
    async def lifespan(app):
        if settings.memory_embedding == "fastembed":
            await asyncio.to_thread(planner.memory.embeddings.encode, ["账户余额查询"])
        await research.recover()
        await chat_tasks.recover()
        await asyncio.to_thread(planner.memory.curator.bootstrap)
        memory_maintenance = asyncio.create_task(planner.memory.curator.maintain()) if planner.memory.curator.enabled else None
        maintenance = asyncio.create_task(chat_tasks.maintain()) if settings.chat_worker_mode=='embedded' else None

        async def scheduler():
            while True:
                await asyncio.sleep(settings.scheduler_interval)
                try:
                    await asyncio.to_thread(events.tick)
                except Exception as error:
                    logging.getLogger(__name__).warning("Scheduler tick failed: %s", type(error).__name__)
                    try:
                        with db.write() as session:
                            record_audit(session, "system", "SCHEDULER_ERROR", {"error_type": type(error).__name__})
                    except Exception:
                        logging.getLogger(__name__).warning("Scheduler audit unavailable")

        if settings.scheduler_mode not in {"external", "embedded", "disabled"}:
            raise BankError("SCHEDULER_CONFIG_INVALID", "调度模式无效")
        task = asyncio.create_task(scheduler()) if settings.scheduler_mode == "embedded" else None
        yield
        if memory_maintenance:
            memory_maintenance.cancel()
            try:
                await memory_maintenance
            except asyncio.CancelledError:
                pass
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if maintenance:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
        await chat_tasks.shutdown()
        await research.shutdown()
        graph.close()
        planner.memory.close()
        db.engine.dispose()

    app = FastAPI(title="简行 · 一句话简化银行办理", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RuntimeMiddleware, telemetry=harness.telemetry)
    app.state.db, app.state.auth, app.state.bank = db, auth, bank
    app.state.plans, app.state.graph, app.state.events, app.state.settings = plans, graph, events, settings
    app.state.memory = planner.memory
    app.state.research = research
    app.state.supervisor = supervisor
    app.state.intake = intake
    app.state.chat_tasks = chat_tasks
    app.state.model_registry = registry
    app.state.workspace = workspace
    app.state.harness = harness

    @app.exception_handler(BankError)
    async def bank_error(_, error):
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code, "message": error.message}})

    def current_user(request: Request, authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise BankError("UNAUTHENTICATED", "请先登录", 401)
        user = auth.authenticate(authorization[7:])
        if request.state.runtime_track and not getattr(request.state, "runtime_owner", None):
            request.state.runtime_owner = user["id"]
            harness.telemetry.emit("request.started", user["id"],
                                   details={"method": request.method, "route": request.scope["route"].path})
        return user

    app.include_router(plan_router(plans, graph, current_user))
    app.include_router(workspace_router(workspace, current_user))

    @app.get("/api/runtime/runs")
    def runtime_runs(user=Depends(current_user)):
        return {"runtime": settings.agent_runtime, "memory_backend": settings.memory_backend,
                "items": harness.list_runs(user["id"])}

    @app.get("/api/runtime/events")
    def runtime_events(after: int = Query(default=0, ge=0, le=9223372036854775807), limit: int = Query(default=100, ge=1, le=200),
                       task_id: str | None = Query(default=None, max_length=160), user=Depends(current_user)):
        return harness.telemetry.events(user["id"], after, limit, task_id)

    @app.get("/api/runtime/metrics")
    def runtime_metrics(hours: int = Query(default=24, ge=1, le=168), user=Depends(current_user)):
        result = harness.telemetry.metrics(user["id"], hours)
        if user.get('demo_admin'):
            result['storage'] = db.diagnostics()
        return result
    from .support import SupportService
    from .support_routes import support_router
    support = SupportService(db, plans, graph, settings)
    app.state.support = support
    app.include_router(support_router(support, current_user))

    @app.post("/api/research/{research_id}/cancel")
    async def cancel_research(research_id: str, user=Depends(current_user)):
        return await research.acancel(user["id"], research_id)

    @app.get("/health")
    def health():
        from .runtime_policy import OWNERSHIP, role_settings
        with db.read() as session:
            heartbeat = next(iter(documents(session, "system_status", "system")), None)
        return {
            "status": "ok",
            'runtime_ownership':OWNERSHIP,
            'effective_role_settings':role_settings(settings),
            'chat_worker_mode':settings.chat_worker_mode,
            "execution_mode": "sandbox",
            "model_mode": settings.model_mode,
            "model_context_tokens": settings.model_context_tokens,
            "task_token_budget": settings.task_token_budget,
            "agent_runtime": settings.agent_runtime,
            "memory_backend": settings.memory_backend,
            "memory_index_mode": "server" if settings.memory_chroma_url else "local",
            "model": settings.model_name,
            "model_configured": bool(settings.model_api_key),
            "model_thinking": settings.model_mode == "llm" and settings.model_thinking,
            "model_reasoning_effort": settings.model_reasoning_effort
            if settings.model_mode == "llm" and settings.model_thinking
            else "none",
            "orchestration": "supervisor_four_specialists" if supervisor else "async_langgraph_verify_backtrack_replan",
            "agent_roles": list(supervisor.roles) if supervisor else list(ROLES),
            "research": {"enabled": True, "roles": ["bills", "wealth"], "max_rounds": research.max_rounds,
                         "read_only": True, "graph": "supervisor_research_synthesize_independent_review"
                         if supervisor else "supervisor_parallel_research_verify_targeted_report"},
            "scheduler_mode": settings.scheduler_mode,
            "process_coordination": "os_file_locks",
            "scheduler_status": {"leader_running": graph.process_locks.busy("scheduler:leader"),
                "last_tick_at": heartbeat.data.get("last_tick_at") if heartbeat else None},
            "memory_embedding": planner.memory.embeddings.backend,
            "memory_model": settings.memory_model,
            "memory_ready": planner.memory.embeddings.model is not None
            or planner.memory.embeddings.backend == "lexical",
            "memory_error": planner.memory.embeddings.error,
        }

    @app.post("/api/auth/login")
    def login(request: LoginRequest):
        return auth.login(request.username, request.password)

    @app.get("/api/auth/me", response_model=PublicUser)
    def me(user=Depends(current_user)):
        return user

    @app.post("/api/auth/logout")
    def logout(authorization: str = Header(default=""), user=Depends(current_user)):
        return auth.logout(authorization[7:])

    @app.get("/api/security/enrollment")
    def enrollment(user=Depends(current_user)):
        return auth.enrollment(user["id"])

    @app.get("/api/security/status")
    def security_status(user=Depends(current_user)):
        return auth.security_status(user["id"])

    @app.post("/api/security/demo-code")
    def demo_code(request: DemoCodeRequest, user=Depends(current_user)):
        result = auth.issue_demo_code(user["id"], **request.model_dump())
        return JSONResponse(content=result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/api/security/unlock")
    def unlock(request: OTPRequest, user=Depends(current_user)):
        return auth.unlock(user["id"], request.otp, request.demo_challenge_id)

    @app.post("/api/security/password")
    def password(request: PasswordRequest, user=Depends(current_user)):
        return auth.change_password(user["id"], request.old_password, request.new_password, request.otp, request.demo_challenge_id)

    @app.get("/api/overview")
    def overview(user=Depends(current_user)):
        facts = bank.facts(user["id"])
        with db.read() as session:
            facts["holds"] = [d.data for d in documents(session, "hold", user["id"])]
            facts["notifications"] = [d.data for d in documents(session, "notification", user["id"])]
            facts["debit_events"] = [d.data for d in documents(session, "debit_event", user["id"])]
        return facts

    def live_snapshot(user_id):
        with db.read() as session:
            from .operations import available, business_now, held_total, primary_account

            account = primary_account(session, user_id)
            plans_rows = list(
                session.scalars(select(Plan).where(Plan.user_id == user_id).order_by(Plan.created_at.desc()).limit(30))
            )
            keys = [plan.id for plan in plans_rows]
            nodes = list(session.scalars(select(Node).where(Node.plan_id.in_(keys)))) if keys else []
            marks = [
                {
                    "id": plan.id,
                    "status": plan.status,
                    "version": plan.version,
                    "signature": hashlib.sha256(
                        canonical([(n.id, n.status, n.fingerprint) for n in nodes if n.plan_id == plan.id]).encode()
                    ).hexdigest(),
                }
                for plan in plans_rows
            ]
            return {
                "type": "snapshot",
                "user_id": user_id,
                "business_now": business_now(session).isoformat(),
                "account": {
                    "balance_fen": account.balance_fen,
                    "reserved_fen": held_total(session, user_id),
                    "available_fen": available(session, user_id),
                },
                "holds": [doc.data for doc in documents(session, "hold", user_id)],
                "notifications": [doc.data for doc in documents(session, "notification", user_id)],
                "plans": marks,
            }

    app.state.live_snapshot = live_snapshot

    @app.get("/api/live")
    async def live(request: Request, authorization: str = Header(default=""), user=Depends(current_user)):
        async def snapshots():
            last = ""
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(auth.authenticate, authorization[7:])
                except BankError:
                    yield json.dumps({"type": "session_expired"}) + "\n"
                    return
                snapshot = await asyncio.to_thread(live_snapshot, user["id"])
                encoded = canonical(snapshot)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                if digest != last:
                    yield encoded + "\n"
                    last = digest
                else:
                    yield '{"type":"heartbeat"}\n'
                await asyncio.sleep(2)

        return StreamingResponse(
            snapshots(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/capabilities")
    def capabilities(user=Depends(current_user)):
        from .capabilities import REGISTRY, CAPABILITY_GAPS
        return {
            **(supervisor.capabilities() if supervisor else {"agents": ROLES}),
            "operations": [cap.describe() for cap in REGISTRY.values()],
            'unavailable_capabilities': CAPABILITY_GAPS,
        }

    @app.post("/api/chat")
    async def chat(request: ChatRequest, user=Depends(current_user)):
        from .storage import uid
        conversation_id = request.conversation_id or uid('conversation')
        from .memory import SENSITIVE
        safe_message = '[敏感凭证已拦截并移除]' if SENSITIVE.search(request.message) else request.message
        with db.write() as session:
            profile, choice = workspace.ensure(session, user['id'], conversation_id,
                                          model_id=request.model_id, message=safe_message)
        with registry.scope(profile.id, require_enabled=bool(request.model_id), explicit=choice.get('model_explicit', False),
                            owner_id=user['id']):
            return await intake.chat(user["id"], request.message, conversation_id)

    @app.post('/api/chat/tasks', status_code=202)
    async def submit_chat_task(request: ChatTaskRequest, user=Depends(current_user)):
        return await chat_tasks.submit(user['id'],request)

    @app.get('/api/chat/tasks/{task_id}')
    def get_chat_task(task_id: str, user=Depends(current_user)):
        return chat_tasks.snapshot(user['id'],task_id)

    @app.post('/api/chat/tasks/{task_id}/cancel')
    async def cancel_chat_task(task_id: str, user=Depends(current_user)):
        return await chat_tasks.cancel(user['id'],task_id)

    @app.get('/api/chat/tasks/{task_id}/events')
    async def chat_task_events(task_id: str, request: Request, authorization: str = Header(default=''),
                               user=Depends(current_user)):
        chat_tasks.snapshot(user['id'],task_id)
        async def stream():
            revision = 0
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(auth.authenticate,authorization[7:])
                except BankError:
                    yield '{"type":"session_expired"}\n'
                    return
                value = await asyncio.to_thread(chat_tasks.snapshot,user['id'],task_id)
                if value['revision'] != revision:
                    revision = value['revision']
                    yield canonical({'type':'progress','task':value}) + '\n'
                if value['status'] in {'SUCCEEDED','FAILED','CANCELLED','INTERRUPTED'}:
                    return
                await asyncio.sleep(.3)
        return StreamingResponse(stream(),media_type='application/x-ndjson',
            headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

    from .goal_state import GoalPatch, apply_goal_patch, read_agenda

    @app.get('/api/conversations/{conversation_id}/goals')
    def conversation_goals(conversation_id: str, user=Depends(current_user)):
        data = read_agenda(graph,user['id'],conversation_id)
        if not data:
            raise BankError('GOAL_NOT_FOUND','这项需求不存在或无权访问',404)
        return data

    @app.post('/api/conversations/{conversation_id}/goals')
    async def update_goal(conversation_id: str, request: GoalPatch, user=Depends(current_user)):
        return await apply_goal_patch(graph,user['id'],conversation_id,request)

    @app.post("/api/research")
    async def start_research(request: ResearchRequest, user=Depends(current_user)):
        from .storage import uid
        conversation_id = request.conversation_id or uid('conversation')
        with db.write() as session:
            from .memory import SENSITIVE
            title = '[敏感凭证已拦截并移除]' if SENSITIVE.search(request.query) else request.query
            profile, choice = workspace.ensure(session, user['id'], conversation_id, message=title)
        request = request.model_copy(update={'conversation_id': conversation_id})
        with registry.scope(profile.id, require_enabled=False, explicit=choice.get('model_explicit', False), owner_id=user['id']):
            public = information_intent(request.query)
            if public:
                return await graph.astart(user["id"], request.query, conversation_id, intent=public)
            return await supervisor.aresearch(user["id"], request) if supervisor else await research.astart(user["id"], request)

    @app.get("/api/research")
    def research_history(user=Depends(current_user)):
        return research.list(user["id"])

    @app.get("/api/research/{research_id}")
    def research_report(research_id: str, user=Depends(current_user)):
        return research.get(user["id"], research_id)

    @app.get("/api/research/{research_id}/evidence/{evidence_id}")
    def research_evidence(research_id: str, evidence_id: str, user=Depends(current_user)):
        return research.evidence(user["id"], research_id, evidence_id)

    @app.post("/api/research/{research_id}/select")
    async def research_select(research_id: str, request: ResearchSelection, user=Depends(current_user)):
        return await research.aselect(user["id"], research_id, request)

    @app.get("/api/scenarios")
    def scenarios(user=Depends(current_user)):
        return [{"id": key, "label": item["label"], "message": item["message"]} for key, item in SCENARIOS.items()]

    @app.get("/api/businesses", response_model=list[BusinessCategory], response_model_exclude_unset=True)
    def business_catalog(user=Depends(current_user)):
        return businesses.catalog(user["id"])

    @app.get("/api/businesses/{category_id}/{action_id}", response_model=BusinessAction, response_model_exclude_unset=True)
    def business_details(category_id: str, action_id: str, user=Depends(current_user)):
        return businesses.catalog(user["id"], category_id, action_id)

    @app.post("/api/businesses/{category_id}/{action_id}/prepare")
    async def prepare_business(category_id: str, action_id: str, request: BusinessRequest, user=Depends(current_user)):
        prepared = await asyncio.to_thread(businesses.prepare, user["id"], category_id, action_id, request.params)
        if prepared["mode"] == "incoming":
            response = await asyncio.to_thread(
                events.prepare_incoming, user["id"], prepared["amount_fen"], request.conversation_id
            )
            from .presentation import yuan

            await asyncio.to_thread(
                graph._record_turn,
                user["id"],
                response["conversation_id"],
                "向本人账户转入" + yuan(prepared["amount_fen"])
                if prepared["amount_fen"] is not None
                else "本人账户转入指引",
                response,
            )
            return response
        await asyncio.to_thread(events.clear_incoming_draft, user["id"], request.conversation_id)
        if prepared["mode"] == "aa_records":
            return {"status": "AA_RECORDS", "items": await asyncio.to_thread(businesses.my_aa, user["id"]),
                    "message": "已整理你参与的AA收款，付款仍需单独核对并确认。", "execution_mode": "sandbox"}
        if prepared["mode"] == "plans":
            return {"status": "PLAN_RECORDS", "items": await asyncio.to_thread(plans.list, user["id"]),
                    "message": "已整理已有办理计划，可查看进度或继续待办。", "execution_mode": "sandbox"}
        if prepared["mode"] == "resume":
            await asyncio.to_thread(plans.retry, user["id"], prepared["plan_id"])
            return await graph.aresume(user["id"], prepared["plan_id"])
        return await graph.astart(user["id"], prepared["message"], request.conversation_id, intent=prepared["intent"])

    @app.post("/api/scenarios/{scenario_id}")
    async def scenario(scenario_id: str, user=Depends(current_user)):
        if scenario_id not in SCENARIOS:
            raise BankError("SCENARIO_NOT_FOUND", "场景不存在", 404)
        item = SCENARIOS[scenario_id]
        intent = Intent(summary=item["message"], tasks=item["tasks"])
        return await graph.astart(user["id"], item["message"], intent=intent)








    @app.get("/api/memory")
    def list_memory(user=Depends(current_user)):
        return planner.memory.list(user["id"])

    @app.post("/api/memory/search")
    def search_memory(request: MemorySearch, user=Depends(current_user)):
        from .memory import protected_context
        from .dialogue import read_context

        facts = bank.facts(user["id"])
        context = read_context(db, user["id"], request.conversation_id, request.query, facts)
        return planner.memory.search(
            user["id"],
            request.query,
            request.role,
            request.conversation_id,
            protected_context(request.query, context, facts),
        )

    @app.get('/api/memory/maintenance')
    def memory_maintenance_status(user=Depends(current_user)):
        return planner.memory.curator.status(user['id'])

    @app.post("/api/memory/preferences")
    def remember_preference(request: PreferenceRequest, user=Depends(current_user)):
        record_id = planner.memory.remember(
            user["id"],
            request.text,
            kind="preference",
            roles=[str(d) for d in request.domains],
            scope="user",
            source_type="user_preference",
            conflict_key=request.key,
            expected_version=request.expected_version,
        )
        return {"id": record_id, "authority": "context_only"}

    @app.get("/api/memory/evidence/{record_id}")
    def memory_evidence(record_id: str, conversation_id: str | None = None, user=Depends(current_user)):
        return planner.memory.reread(user["id"], record_id, conversation_id=conversation_id)

    @app.delete("/api/memory/{record_id}")
    def forget_memory(record_id: str, user=Depends(current_user)):
        planner.memory.forget(user["id"], record_id)
        return {"status": "DELETED"}


    @app.post("/api/demo/payroll")
    def payroll(request: PayrollRequest, user=Depends(current_user)):
        return events.payroll(user["id"], request.event_id, request.amount_fen)

    @app.post("/api/demo/incoming/{request_id}/decision")
    def incoming_decision(request_id: str, request: IncomingDecision, user=Depends(current_user)):
        return events.decide_incoming(user["id"], request_id, request.decision)

    @app.post("/api/demo/clock")
    def clock(request: ClockRequest, user=Depends(current_user)):
        return events.advance(user["id"], request.days)

    @app.post("/api/scheduler/tick")
    def tick(user=Depends(current_user)):
        # Only local admin can trigger jobs for all users.
        with db.read() as session:
            events.require_demo_admin(session, user["id"])
        return events.tick()

    @app.get("/api/audit")
    def audit(user=Depends(current_user)):
        return plans.audit_log(user["id"])

    @app.post("/api/analysis/sandbox")
    def statistics(request: AnalysisRequest, user=Depends(current_user)):
        with db.read() as session:
            data = bank.dispatch(
                session, user["id"], "bills.report", {"period": request.period, "annual": False}, "analysis"
            )
        return run_statistics(request.code, data)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    return app
