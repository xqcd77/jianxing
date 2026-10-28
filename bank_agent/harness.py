"""One bounded, typed runtime for all model roles.

The SDK only produces proposals. Bank permissions, durable workflow checkpoints,
financial execution and authoritative evidence stay outside the model runtime.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from time import perf_counter

from sqlalchemy import select

from .errors import BankError
from .storage import AgentRun, now_iso, uid
from .runtime_context import task_state as _task_context
from .runtime import RuntimeTelemetry, append_event
from .token_budget import RequestTokenBudget, TaskTokenBudget


def task_context(function):
    """Bind trusted service arguments, never model-supplied identity fields."""
    @wraps(function)
    async def wrapped(self, user_id, *args, **kwargs):
        previous = _task_context.get()
        if previous.owner_id and previous.owner_id != user_id:
            raise BankError("HARNESS_OWNER_MISMATCH", "任务主体不一致", 403)
        token = _task_context.set(replace(previous, owner_id=user_id, task_id=previous.task_id or uid('task')))
        try:
            return await function(self, user_id, *args, **kwargs)
        finally:
            _task_context.reset(token)
    return wrapped


class BankAgentHarness:
    def __init__(self, settings, db=None):
        if settings.agent_runtime not in {"agents_sdk", "legacy_http"}:
            raise BankError("HARNESS_CONFIG_INVALID", "Agent运行时配置无效", 503)
        self.settings, self.db = settings, db
        self.telemetry = RuntimeTelemetry(db)
        self.budget = RequestTokenBudget(settings)
        self.task_budget = TaskTokenBudget(settings)
        self.context_builder = None
        self.model_registry = None
        self.role_returns = None
        self.stage_packer = None

    def prepare_payload(self, role, payload):
        context = _task_context.get()
        if self.context_builder is None or not context.owner_id:
            return payload
        prepared = self.context_builder.prepare_payload(role, payload, context.owner_id, context.conversation_id)
        brief = prepared['task_brief']
        self.telemetry.emit('context.prepared', role=role, details={
            'method': 'task_brief_v1', 'plan_id': brief.get('plan_id') or '',
            'version': brief.get('plan_version') or 0, 'candidate_count': len(brief['goals']),
            'hit_count': len(brief['goals']) - len(brief['closed_goal_ids']),
        })
        return prepared

    def _reserve_tokens(self, record, budget):
        if self.db is not None:
            with self.db.write() as session:
                self.task_budget.reserve(session, record, budget)

    @contextmanager
    def scope(self, owner_id, conversation_id="", task_id=""):
        previous = _task_context.get()
        if previous.owner_id and previous.owner_id != owner_id:
            raise BankError("HARNESS_OWNER_MISMATCH", "任务主体不一致", 403)
        token = _task_context.set(replace(previous, owner_id=owner_id,
            conversation_id=conversation_id or previous.conversation_id, task_id=task_id or previous.task_id or uid('task')))
        try:
            yield
        finally:
            _task_context.reset(token)

    def _start(self, role, schema, request):
        from .progress import publish_progress
        labels = {'business':'正在整理办理步骤','planner':'正在整理办理步骤','verifier':'正在核验需求与办理约束',
                  'route':'正在选择处理路径','bills_research':'正在核查账单证据','wealth_research':'正在核查产品证据'}
        publish_progress('model',labels.get(role,'正在核查资料与整理结果'))
        context = _task_context.get()
        record = {
            "id": uid("agent_run"), "owner_id": context.owner_id,
            "conversation_id": context.conversation_id, "task_id": context.task_id,
            "role": role, "schema_name": schema.__name__, "runtime": self.settings.agent_runtime,
            "model_name": request["model"], "status": "RUNNING", "created_at": now_iso(),
        }
        if self.db is not None:
            with self.db.write() as session:
                session.add(AgentRun(**record))
                append_event(session, "run.started", context.owner_id, run_id=record["id"], role=role,
                             details={"runtime": self.settings.agent_runtime, "schema_name": schema.__name__})
        return record

    def _finish(self, record, status, duration_ms, usage, error_code=""):
        if self.db is None:
            return
        # Missing provider accounting stays NULL, never a fabricated zero.
        def count(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None
        with self.db.write() as session:
            row = session.get(AgentRun, record["id"])
            row.status, row.duration_ms = status, round(duration_ms, 3)
            row.prompt_tokens, row.completion_tokens = count("prompt_tokens"), count("completion_tokens")
            row.total_tokens = count("total_tokens")
            row.error_code, row.finished_at = error_code, now_iso()
            self.task_budget.settle(session, record, count("total_tokens"))
            append_event(session, "run.finished", row.owner_id, run_id=row.id, role=row.role,
                         details={"status": status, "duration_ms": row.duration_ms, "error_code": error_code,
                                  **{key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}})

    async def run(self, role, request, schema, invoke, decode):
        runtime_settings = self.model_registry.effective(self.settings) if self.model_registry else self.settings
        budget_service = RequestTokenBudget(runtime_settings) if _task_context.get().model_id else self.budget
        if self.settings.orchestration_mode == "supervisor":
            role = {"planner": "business", "bills": "bills_research", "wealth": "wealth_research",
                    "route": "supervisor", "details": "supervisor", "analyst": "supervisor"}.get(role, role)
        record = None
        usage, status, error_code = {}, "FAILED", ""
        started = perf_counter()

        async def proposal():
            await asyncio.to_thread(self.telemetry.emit, "model.requested", run_id=record["id"], role=role)
            record["_provider_started"] = True
            response = await invoke()
            try:
                raw_usage = response.json().get("usage", {})
                if isinstance(raw_usage, dict):
                    usage.update(raw_usage)
            except (ValueError, AttributeError):
                pass
            await asyncio.to_thread(self.telemetry.emit, "model.responded", run_id=record["id"], role=role,
                                    details={"provider_status": response.status_code})
            return decode(response, schema)

        try:
            creation = asyncio.create_task(asyncio.to_thread(self._start, role, schema, request))
            try:
                record = await asyncio.shield(creation)
            except asyncio.CancelledError:
                # A SQL write in a worker cannot be cancelled halfway through.
                # Wait for its ID so the row is closed as CANCELLED below.
                record = await creation
                raise
            async with asyncio.timeout(runtime_settings.model_timeout):
                try:
                    prepared, budget = budget_service.prepare(request)
                    reservation = asyncio.create_task(asyncio.to_thread(self._reserve_tokens, record, budget))
                    try:
                        await asyncio.shield(reservation)
                    except asyncio.CancelledError:
                        await reservation
                        raise
                except BankError as error:
                    if hasattr(error, "budget"):
                        await asyncio.to_thread(self.telemetry.emit, "budget.rejected", run_id=record["id"],
                                                role=role, details=error.budget)
                    raise
                request.clear()
                request.update(prepared)  # The transport closure sees this exact budgeted request.
                await asyncio.to_thread(self.telemetry.emit, "budget.checked", run_id=record["id"], role=role, details=budget)
                if budget["removed_items"] or budget["removed_turns"]:
                    await asyncio.to_thread(self.telemetry.emit, "budget.trimmed", run_id=record["id"], role=role, details=budget)
                if self.settings.agent_runtime == "legacy_http":
                    result = await proposal()
                else:
                    result = await self._sdk_run(role, request, schema, proposal, usage)
            status = "SUCCEEDED"
            return result
        except asyncio.CancelledError:
            status, error_code = "CANCELLED", "TASK_CANCELLED"
            raise
        except TimeoutError:
            status, error_code = "TIMED_OUT", "MODEL_DEADLINE_EXCEEDED"
            raise BankError(error_code, "本次理解需求超时，没有执行业务。请核实当前计划后重试。", 502) from None
        except BankError as error:
            error_code = error.code
            raise
        except Exception as error:
            # No provider body, arbitrary exception text, prompts or reasoning in logs.
            error_code = "HARNESS_RESPONSE_INVALID"
            raise BankError("MODEL_RESPONSE_INVALID", "Agent输出未通过校验，没有执行业务", 502) from error
        finally:
            if record is not None:
                completion = asyncio.create_task(asyncio.to_thread(
                    self._finish, record, status, (perf_counter() - started) * 1000, usage, error_code,
                ))
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    await completion
                    raise

    def run_sync(self, *args, **kwargs):
        return asyncio.run(self.run(*args, **kwargs))

    @staticmethod
    async def _sdk_run(role, request, schema, proposal, usage):
        from agents import Agent, AgentOutputSchema, RunConfig, Runner
        from .sdk_model import ProposalModel
        from .runtime_policy import SDK_MAX_TURNS

        agent = Agent(
            name=role,
            instructions=request["messages"][0]["content"],
            model=ProposalModel(proposal, usage),
            output_type=AgentOutputSchema(schema, strict_json_schema=False),
            tools=[],  # Proposal functions are not executable banking tools.
        )
        result = await Runner.run(
            agent,
            input=request["messages"][-1]["content"],
            max_turns=SDK_MAX_TURNS,
            run_config=RunConfig(tracing_disabled=True),  # Metadata-only local records.
        )
        return result.final_output_as(schema)

    def list_runs(self, owner_id, limit=50):
        if self.db is None:
            return []
        with self.db.read() as session:
            rows = session.scalars(select(AgentRun).where(AgentRun.owner_id == owner_id)
                                   .order_by(AgentRun.created_at.desc()).limit(max(1, min(limit, 100))))
            return [{column.name: getattr(row, column.name) for column in AgentRun.__table__.columns
                     if column.name != "owner_id"} for row in rows]
