"""Async LangGraph orchestration with immutable receipts and versioned business DAGs."""

from collections import defaultdict
import operator
import re
import asyncio
import aiosqlite
from datetime import datetime, timedelta, timezone
from threading import Thread
from weakref import WeakValueDictionary
from typing import Annotated, TypedDict
from sqlalchemy import select

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from .agents import DOMAIN_AGENTS, ReviewerAgent, expand_tasks
from .errors import ClarificationNeeded
from .presentation import clarification_reply
from .conversation import answer_reply
from .information import PublicInformationService, scoped_information_intent
from .dialogue import amount_answer, money_slot, read_context, record_turn
from .schemas import Intent, NodeSpec, PlanEdit, ReplanRequest
from .storage import Document, Plan, User, audit, save_document, uid
from .memory import protected_context, SENSITIVE
from .orchestration import DynamicOrchestrator, Phase
from .harness import task_context


class State(TypedDict, total=False):
    user_id: str
    conversation_id: str
    thread_id: str
    message: str
    intent: dict
    specs: list[dict]
    role: str
    proposals: Annotated[list[dict], operator.add]
    review: dict
    plan_id: str
    response: dict
    execution_status: str
    verification: dict
    backtrack: dict


class BankingGraph:
    def __init__(self, data_dir, planner, plans, bank):
        self.planner, self.plans = planner, plans
        self.information = PublicInformationService(planner.db)
        self.agents = {key: cls(key, bank) for key, cls in DOMAIN_AGENTS.items()}
        self.reviewer = ReviewerAgent()
        self.orchestrator = DynamicOrchestrator(plans, planner.memory, planner.settings)
        self.process_locks = plans.locks
        self._async_locks = WeakValueDictionary()
        self.loop = asyncio.new_event_loop()
        self.runtime = Thread(target=self._run_loop, name="bank-langgraph", daemon=True)
        self.runtime.start()
        builder = StateGraph(State)
        builder.add_node("planner", self._aplan)
        builder.add_node("domain_agent", self._adomain)
        builder.add_node("reviewer", self._areview)
        builder.add_node("compile_plan", self._acompile)
        builder.add_node("verify", self._verify)
        builder.add_node("backtrack", self._backtrack)
        builder.add_node("replan", self._replan)
        builder.add_node("execute", self._aexecute)
        builder.add_node("await_input", self._await_input)
        builder.add_node("finish", self._finish)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges("planner", self._dispatch, ["domain_agent", END])
        builder.add_edge("domain_agent", "reviewer")
        builder.add_edge("reviewer", "compile_plan")
        builder.add_edge("compile_plan", "verify")
        builder.add_conditional_edges("verify", self._verify_route, ["backtrack", "execute", "await_input"])
        builder.add_conditional_edges("backtrack", self._backtrack_route, ["replan", "verify", "await_input"])
        builder.add_edge("replan", "verify")
        builder.add_conditional_edges("execute", self._execution_route, ["await_input", "finish"])
        builder.add_edge("await_input", "verify")
        builder.add_edge("finish", END)
        self._submit(self._initialize(data_dir, builder))

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    async def _initialize(self, data_dir, builder):
        self.connection = await aiosqlite.connect(str(data_dir / "graph.sqlite3"))
        self.checkpointer = AsyncSqliteSaver(self.connection)
        self.graph = builder.compile(checkpointer=self.checkpointer)

    def _submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def _async_lock(self, key):
        lock = self._async_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._async_locks[key] = lock
        return lock

    @staticmethod
    async def _atomic(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A bank transaction completes or rolls back before cancellation propagates.
            await task
            raise

    async def _aplan(self, state):
        try:
            return await self._aplan_unchecked(state)
        except ClarificationNeeded as error:
            intent = Intent(summary=state["message"], clarification=error.message)
            return await self._atomic(self._plan, {**state, "intent": intent.model_dump(mode="json")})

    async def _aplan_unchecked(self, state):
        from .progress import publish_progress
        publish_progress('planning','正在逐项核对需求与办理条件')
        intent = (
            Intent.model_validate(state["intent"])
            if state.get("intent")
            else await self.planner.aplan(
                state["user_id"],
                state["message"],
                state["conversation_id"],
            )
        )
        if intent.information_query:
            response = await self.information.answer(intent.information_query)
            return {"intent": intent.model_dump(mode="json"), "response": response}
        return await self._atomic(self._plan, {**state, "intent": intent.model_dump(mode="json")})

    async def _adomain(self, state):
        return await asyncio.to_thread(self._domain, state)

    async def _areview(self, state):
        return await asyncio.to_thread(self._review, state)

    async def _acompile(self, state):
        from .progress import publish_progress
        publish_progress('verifying','正在核对完整计划与实时资金条件')
        return await self._atomic(self._compile, state)

    async def _verify(self, state):
        user_id, plan_id = state["user_id"], state["plan_id"]
        await self._atomic(self.orchestrator.phase, user_id, plan_id, Phase.VERIFYING)
        report = await self._atomic(self.orchestrator.verify, user_id, plan_id)
        if report["repairable"] and report["repair_count"] >= max(0, min(self.planner.settings.max_replans, 5)):
            await self._atomic(self.plans.handoff, user_id, plan_id, "REPAIR_LIMIT")
            await self._atomic(self.orchestrator.phase, user_id, plan_id, Phase.HUMAN)
            report["halted"] = True
        return {"verification": report, **({"execution_status": "WAIT_HUMAN"} if report.get("halted") else {})}

    @staticmethod
    def _verify_route(state):
        report = state["verification"]
        if report.get("halted"):
            return "await_input"
        return (
            "backtrack"
            if report["repairable"] or (report["reread_required"] and not report["evidence_checked"])
            else "execute"
        )

    async def _backtrack(self, state):
        user_id, plan_id = state["user_id"], state["plan_id"]
        await self._atomic(self.orchestrator.phase, user_id, plan_id, Phase.BACKTRACKING)
        result = await self._atomic(self.orchestrator.backtrack, user_id, plan_id)
        if result["requires_human"]:
            await self._atomic(self.orchestrator.phase, user_id, plan_id, Phase.HUMAN)
        return {"backtrack": result, **({"execution_status": "WAIT_HUMAN"} if result["requires_human"] else {})}

    @staticmethod
    def _backtrack_route(state):
        if state["backtrack"]["requires_human"]:
            return "await_input"
        return "replan" if state["verification"]["repairable"] else "verify"

    async def _replan(self, state):
        await self._atomic(self.orchestrator.phase, state["user_id"], state["plan_id"], Phase.REPLANNING)
        await self._atomic(self.orchestrator.replan, state["user_id"], state["plan_id"])
        return {}

    async def _aexecute(self, state):
        from .progress import publish_progress
        publish_progress('banking','正在核对办理状态，资金操作仍按确认规则执行')
        await self._atomic(self.orchestrator.phase, state["user_id"], state["plan_id"], Phase.EXECUTING)
        result = await self._atomic(self._execute, state)
        phase = (
            Phase.FINISHED
            if result["execution_status"] in {"COMPLETED", "CANCELLED", "PARTIAL"}
            else Phase.AWAITING_INPUT
        )
        await self._atomic(self.orchestrator.phase, state["user_id"], state["plan_id"], phase)
        return result

    def _plan(self, state):
        try:
            intent = (
                Intent.model_validate(state["intent"])
                if state.get("intent")
                else self.planner.plan(state["user_id"], state["message"], state["conversation_id"])
            )
        except ClarificationNeeded as error:
            intent = Intent(summary=state["message"], clarification=error.message)
        from .intent_constraints import constrain_intent
        intent = constrain_intent(intent, state["message"])
        from .dialogue import partition_incomplete_tasks
        intent = partition_incomplete_tasks(intent,state['message'])
        if intent.additional_goals and not intent.tasks and not intent.reject_reason and not intent.clarification:
            return {'intent':intent.model_dump(mode='json'),'response':{
                'status':'ANSWERED','message':'已逐项保留本次需求，处理情况如下。','execution_mode':'sandbox'}}
        if intent.answer:
            reply = answer_reply(intent.answer, self.planner.settings, state["message"])
            if intent.answer.kind in {"capabilities", "explanation"}:
                context = read_context(self.planner.db, state["user_id"], state["conversation_id"], state["message"], self.planner.bank.facts(state["user_id"]))
                active = context.get("active_plan", {})
                if active and active.get("status") not in {"COMPLETED", "CANCELLED"}:
                    reply += "\n原办理计划仍保留，可回到原计划核对后继续确认，无需重复填写；本次咨询没有替你执行原操作。"
            return {
                "intent": intent.model_dump(mode="json"),
                "response": {
                    "status": "ANSWERED",
                    "message": reply,
                    "answer_kind": intent.answer.kind,
                    "execution_mode": "sandbox",
                },
            }
        if intent.clarification or intent.reject_reason or not intent.tasks:
            if not intent.reject_reason:
                draft_context = read_context(
                    self.planner.db,
                    state["user_id"],
                    state["conversation_id"],
                    state["message"],
                    self.planner.bank.facts(state["user_id"]),
                )
                with self.planner.db.write() as session:
                    key = f"draft_{state['user_id']}_{state['conversation_id']}"
                    save_document(
                        session,
                        key,
                        "draft",
                        state["user_id"],
                        {
                            "request": draft_context.get("pending_request", state["message"]),
                            "clarification": intent.clarification,
                            "clarification_kind": intent.clarification_kind,
                            "intent": intent.model_dump(mode="json"),
                            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                        },
                    )
            if intent.reject_reason and intent.rejection_kind == "security":
                with self.planner.db.write() as session:
                    key = f"suspicion_{state['user_id']}"
                    previous = session.get(Document, key)
                    attempts = (previous.data["attempts"] if previous else 0) + 1
                    save_document(session, key, "suspicion", state["user_id"], {"attempts": attempts})
                    if attempts >= 3:
                        session.get(User, state["user_id"]).locked = True
                    audit(
                        session,
                        state["user_id"],
                        "UNSAFE_REQUEST_REJECTED",
                        {"reason": intent.reject_reason, "attempts": attempts, "locked": attempts >= 3},
                    )
            return {
                "intent": intent.model_dump(mode="json"),
                "response": {
                    "status": "REJECTED" if intent.reject_reason else "NEEDS_CLARIFICATION",
                    "message": clarification_reply(intent),
                    "execution_mode": "sandbox",
                },
            }
        try:
            specs = expand_tasks(intent, self.planner.bank.facts(state["user_id"])["business_now"])
        except ClarificationNeeded as error:
            return {"response": {"status": "NEEDS_CLARIFICATION", "message": error.message}}
        with self.planner.db.write() as session:
            draft = session.get(Document, f"draft_{state['user_id']}_{state['conversation_id']}")
            if draft:
                session.delete(draft)
        return {"intent": intent.model_dump(mode="json"), "specs": [n.model_dump(mode="json") for n in specs]}

    def _dispatch(self, state):
        if state.get("response"):
            return END
        groups = defaultdict(list)
        for spec in state["specs"]:
            groups[spec["agent"]].append(spec)
        return [
            Send(
                "domain_agent",
                {
                    "role": role,
                    "specs": specs,
                    "user_id": state["user_id"],
                    "conversation_id": state["conversation_id"],
                    "message": state["message"],
                },
            )
            for role, specs in groups.items()
        ]

    def _domain(self, state):
        proposal = self.agents[state["role"]].propose(
            [NodeSpec.model_validate(n) for n in state["specs"]], state["user_id"]
        )
        context = self.planner.memory.search_for_request(
            state["user_id"],
            state["message"],
            role=state["role"],
            conversation_id=state["conversation_id"],
        )
        proposal.facts.append({"shared_memory": context})
        return {"proposals": [proposal.model_dump(mode="json")]}

    def _review(self, state):
        by_id = {n["id"]: NodeSpec.model_validate(n) for p in state["proposals"] for n in p["nodes"]}
        specs = [by_id[n["id"]] for n in state["specs"]]
        review = self.reviewer.review(specs)
        review["agent_findings"] = {p["agent"]: p["facts"] for p in state["proposals"]}
        facts = self.planner.bank.facts(state["user_id"])
        context = read_context(self.planner.db, state["user_id"], state["conversation_id"], state["message"], facts)
        protected = protected_context(state["message"], context, facts)
        review["protected"] = protected.model_dump(mode="json")
        review["memory"] = self.planner.memory.search_for_request(
            state["user_id"],
            state["message"],
            conversation_id=state["conversation_id"],
            protected=protected,
        )
        return {"review": review, "specs": [n.model_dump(mode="json") for n in specs]}

    def _compile(self, state):
        from decimal import Decimal

        product_names = {p["id"]: p["name"] for p in self.planner.bank.facts(state["user_id"])["products"]}

        labels = {
            "funds": "资金办理",
            "bills": "账单分析",
            "wealth": "理财办理",
            "cards": "卡片管理",
            "subscriptions": "订阅管理",
            "life": "生日安排",
        }
        descriptions = []
        for task in state["intent"]["tasks"]:
            params = task["params"]
            label = labels[task["domain"]]
            if task["action"] == "funds.transfer":
                label = "转账给" + params.get("recipient_hint", "待确认收款人")
            if task["action"] == "funds.reserve":
                label = "预留预算"
            if task["action"] in ["wealth.purchase", "wealth.redeem"]:
                label = ("申购" if task["action"].endswith("purchase") else "赎回") + product_names.get(
                    params.get("product_id"), "待选产品"
                )
            amount = params.get("amount_fen", params.get("budget_fen"))
            if isinstance(amount, int):
                label += f" {Decimal(amount) / 100:.2f}元"
            descriptions.append(label)
        # The model's summary may contain a false success claim. Public plan titles
        # describe proposed operations; completed financial facts come from receipts.
        summary = "办理计划：" + "；".join(descriptions)
        key = self.plans.create(
            state["user_id"],
            state["conversation_id"],
            state["thread_id"],
            summary,
            [NodeSpec.model_validate(n) for n in state["specs"]],
            state["review"],
        )
        return {"plan_id": key}

    def _execute(self, state):
        return {"execution_status": self.plans.step(state["user_id"], state["plan_id"])}

    @staticmethod
    def _execution_route(state):
        return "finish" if state["execution_status"] in ["COMPLETED", "CANCELLED", "PARTIAL"] else "await_input"

    @staticmethod
    def _await_input(state):
        # A replay-safe, single interrupt with no banking side effects before it.
        # The authenticated endpoint writes a grant; the resume marker grants no authority.
        interrupt(
            {
                "plan_id": state["plan_id"],
                "status": state["execution_status"],
                "next": "use_authenticated_plan_endpoint",
            }
        )
        return {}

    def _finish(self, state):
        return {"response": self.plans.snapshot(state["user_id"], state["plan_id"])}

    def start(self, user_id, message, conversation_id=None, intent=None):
        return self._submit(self._astart(user_id, message, conversation_id, intent))

    async def astart(self, user_id, message, conversation_id=None, intent=None, request_key=None):
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                self._astart(user_id, message, conversation_id, intent, thread_id=request_key),
                self.loop,
            )
        )

    @task_context
    async def _astart(self, user_id, message, conversation_id, intent, thread_id=None):
        from .runtime_context import task_state
        task_id = task_state.get().task_id
        if thread_id is None and task_id.startswith('chat_task_'):
            thread_id = task_id
        conversation_id = conversation_id or uid("conversation")
        with self.planner.model.harness.scope(user_id, conversation_id):
            async with self._async_lock(f"conversation:{user_id}:{conversation_id}"):
                async with self.process_locks.ahold(f"conversation:{user_id}:{conversation_id}"):
                    return await self._start(user_id, message, conversation_id, intent, thread_id)

    def _short_reply(self, user_id, message, conversation_id, intent):
        if intent is None and amount_answer(message):
            return self._amount_reply(user_id, message, conversation_id)
        return None

    def public_information_intent(self, user_id, message, conversation_id=None):
        from .intake import request_manifest
        if len({g['kind'] for g in request_manifest(message)})>1:
            return None
        return scoped_information_intent(self.planner.db, user_id, message, conversation_id)

    def _record_turn(self, user_id, conversation_id, message, response, intent=None):
        record_turn(self.planner.db, user_id, conversation_id, message, response, intent)
        self.planner.memory.capture(user_id, conversation_id, message, response, intent)

    async def _start(self, user_id, message, conversation_id, intent, thread_id=None):
        if thread_id:
            with self.planner.db.read() as session:
                existing = session.scalar(select(Plan).where(Plan.thread_id == thread_id, Plan.user_id == user_id))
                if existing:
                    return self.plans.snapshot(user_id, existing.id)
        if SENSITIVE.search(message):
            intent = Intent(
                summary="敏感凭证已拦截",
                reject_reason="请在安全中心输入凭证；不要发送至聊天",
                rejection_kind="security",
            )
            message = "[敏感凭证已拦截并移除]"
        conversation_id = conversation_id or uid("conversation")
        if intent is None:
            intent = await asyncio.to_thread(self.public_information_intent, user_id, message, conversation_id)
        short = await asyncio.to_thread(self._short_reply, user_id, message, conversation_id, intent)
        if short:
            return short
        if intent is None:
            appended, intent = await self._maybe_append(user_id, message, conversation_id)
            if appended:
                return appended
        thread_id = thread_id or uid("thread")
        result = await self._invoke_locked(
            {
                "user_id": user_id,
                "message": message,
                "thread_id": thread_id,
                "conversation_id": conversation_id,
                "proposals": [],
                **({"intent": intent.model_dump(mode="json")} if intent else {}),
            },
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 256},
        )
        response = (
            await asyncio.to_thread(self.plans.snapshot, user_id, result["plan_id"])
            if result.get("plan_id")
            else {**result["response"], "conversation_id": result["conversation_id"]}
        )
        from .request_goals import complete_goal_agenda
        response = await complete_goal_agenda(self, user_id, conversation_id, response, result.get('intent'))
        await self._atomic(self._record_turn, user_id, conversation_id, message, response, result.get("intent"))
        return response

    async def _invoke_locked(self, values, config):
        async with self.process_locks.ahold("graph:" + config["configurable"]["thread_id"]):
            return await self.graph.ainvoke(values, config)

    async def _maybe_append(self, user_id, message, conversation_id):
        facts = await asyncio.to_thread(self.planner.bank.facts, user_id)
        context = await asyncio.to_thread(read_context, self.planner.db, user_id, conversation_id, message, facts)
        original = context.get("pending_request", message)
        if not re.match(
            r"\s*(?:在(?:这个|当前)(?:计划|安排)(?:里|中)?[，,\s]*)?(?:再加|追加|加上|再给|再帮我|再安排)", original
        ):
            return None, None
        if not context.get("active_plan_id"):
            return None, None
        plan = await asyncio.to_thread(self.plans.snapshot, user_id, context["active_plan_id"])
        if plan["status"] in {"COMPLETED", "CANCELLED", "PARTIAL"}:
            return None, None
        intent = await self.planner.aplan(user_id, message, conversation_id)
        if intent.clarification or intent.reject_reason or not intent.tasks:
            return None, intent
        extra = expand_tasks(intent, facts["business_now"])
        names = {node.id: f"r{plan['version'] + 1}_{i}" for i, node in enumerate(extra)}

        def rename(value):
            if isinstance(value, str) and value.startswith("$"):
                from .dag import references

                key, field = next(references(value))
                return "$" + names.get(key, key) + "." + field
            if isinstance(value, dict):
                return {k: rename(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rename(v) for v in value]
            return value

        existing = [
            NodeSpec(
                id=n["local_id"],
                agent=n["agent"],
                operation=n["operation"],
                params=n["params"],
                depends_on=n["depends_on"],
                after=n["after"],
                scheduled_for=n["scheduled_for"],
                reason=n["reason"],
            )
            for n in plan["nodes"]
        ]
        added = [
            n.model_copy(
                update={
                    "id": names[n.id],
                    "params": rename(n.params),
                    "depends_on": [names.get(d, d) for d in n.depends_on],
                    "after": [names.get(d, d) for d in n.after],
                }
            )
            for n in extra
        ]
        blocked = await self._validate_append(user_id, message, conversation_id, plan, existing, added, context, facts)
        if blocked:
            await self._atomic(self._record_turn, user_id, conversation_id, message, blocked, intent.model_dump(mode="json"))
            return blocked, intent
        await self._atomic(
            self.plans.replan,
            user_id,
            plan["id"],
            ReplanRequest(
                expected_version=plan["version"],
                reason="用户追加办理安排",
                nodes=existing + added,
            ),
            False,
            protected_context(message, context, facts),
        )
        response = await self._aresume(user_id, plan["id"])
        await self._atomic(
            self._record_turn, user_id, conversation_id, message, response, intent.model_dump(mode="json")
        )
        return response, intent

    async def _validate_append(self, user_id, message, conversation_id, plan, existing, added, context, facts):
        """New runtimes may review interpreted additions before changing a plan."""
        return None

    def _amount_reply(self, user_id, message, conversation_id):
        if amount_answer(message):
            context = read_context(self.planner.db, user_id, conversation_id, message, self.planner.bank.facts(user_id))
            if context.get("active_plan_id") and not context.get("pending_draft"):
                current = self.plans.snapshot(user_id, context["active_plan_id"])
                transfers = [n for n in current["nodes"] if n["operation"] == "funds.transfer"]
                if len(transfers) == 1 and money_slot(message) == transfers[0]["params"]["amount_fen"]:
                    self._record_turn(user_id, conversation_id, message, current)
                    return current
                if (
                    len(transfers) == 1
                    and re.match(r"\s*改(?:成|为)", message)
                    and transfers[0]["status"] not in ["SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"]
                    and current["status"] != "CANCELLED"
                ):
                    amount = money_slot(message)
                    if amount is not None:
                        self.plans.edit(
                            user_id,
                            current["id"],
                            PlanEdit(
                                node_id=transfers[0]["id"], params={**transfers[0]["params"], "amount_fen": amount}
                            ),
                        )
                        updated = self.resume(user_id, current["id"])
                        self._record_turn(user_id, conversation_id, message, updated)
                        return updated
        return None

    def resume(self, user_id, plan_id):
        return self._submit(self._aresume(user_id, plan_id))

    async def aresume(self, user_id, plan_id):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._aresume(user_id, plan_id), self.loop))

    @task_context
    async def _aresume(self, user_id, plan_id):
        snapshot = await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
        with self.planner.model.harness.scope(user_id, snapshot["conversation_id"], plan_id):
            async with self.process_locks.ahold("graph:" + snapshot["thread_id"]):
                return await self._resume_locked(user_id, plan_id)

    async def _resume_locked(self, user_id, plan_id):
        snapshot = await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
        config = {"configurable": {"thread_id": snapshot["thread_id"]}, "recursion_limit": 256}
        async with self._async_lock(snapshot["thread_id"]):
            state = await self.graph.aget_state(config)
            if state.next:
                if state.values.get("user_id") != user_id:
                    from .errors import BankError

                    raise BankError("GRAPH_OWNER_MISMATCH", "检查点用户与计划主体不一致", 403)
                await self.graph.ainvoke(Command(resume={"continue": True}), config)
            else:
                await self._atomic(self.plans.step, user_id, plan_id)
        snapshot = await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
        await self._atomic(self.planner.memory.capture_results, user_id, snapshot["conversation_id"], snapshot)
        return snapshot

    async def amutate(self, user_id, plan_id, action, request=None):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
            self._mutate(user_id, plan_id, action, request), self.loop))

    @task_context
    async def _mutate(self, user_id, plan_id, action, request):
        snapshot = await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
        async with self.process_locks.ahold("graph:" + snapshot["thread_id"]):
            if action == "approve":
                report = await asyncio.to_thread(self.orchestrator.verify, user_id, plan_id)
                if report["repairable"]:
                    await self._aresume(user_id, plan_id)
                await self._atomic(self.plans.authorize, user_id, plan_id, request)
            elif action == "approve-batch":
                await self._atomic(self.plans.authorize_batch, user_id, plan_id, request)
            elif action == "edit":
                await self._atomic(self.plans.edit, user_id, plan_id, request)
            elif action == "replan":
                await self._atomic(self.plans.replan, user_id, plan_id, request)
            elif action == "resume":
                await self._atomic(self.plans.retry, user_id, plan_id)
            elif action == "reconcile":
                await self._atomic(self.plans.reconcile, user_id, plan_id)
            elif action == "cancel":
                await self._atomic(self.plans.cancel, user_id, plan_id)
            elif action == "cancel-node":
                await self._atomic(self.plans.cancel_node, user_id, plan_id, request)
            elif action == "handoff":
                await self._atomic(self.plans.handoff, user_id, plan_id)
                return await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
            else:
                from .errors import BankError
                raise BankError("ACTION_NOT_ALLOWED", "不支持这项计划操作")
            result=await self._aresume(user_id, plan_id)
            from .request_goals import activate_plan_agenda
            await self._atomic(activate_plan_agenda,self.planner.db,user_id,result)
            return await asyncio.to_thread(self.plans.snapshot,user_id,plan_id)

    def close(self):
        if self.loop.is_running():
            self._submit(self.connection.close())
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.runtime.join(timeout=30)
