"""One Supervisor coordinates direct handling and read-only deep research."""

import asyncio
import re

from .deep_banking import educational_answer
from .errors import BankError
from .harness import task_context
from .memory import SENSITIVE
from .research import UNSAFE
from .research_schemas import ResearchRequest
from .storage import audit
from .supervisor_agents import CORE_ROLES, RouteDecision


class Supervisor:
    roles = CORE_ROLES

    def __init__(self, banking, research):
        self.banking, self.research = banking, research
        # Brief extraction, ambiguous routing and synthesis are the same role;
        # none retains a private cross-user chat transcript.
        self.model = research.model
        self.settings = research.settings

    async def decide(self, user_id, message, conversation_id=None):
        # Global exclusions precede all research patterns. A later positive regex
        # cannot turn a rejected permission-bypass request into a research job.
        if SENSITIVE.search(message) or UNSAFE.search(message) or educational_answer(message):
            return RouteDecision(path="direct", reason="安全检查或业务解释")
        public = await asyncio.to_thread(self.banking.public_information_intent, user_id, message, conversation_id)
        if public:
            return RouteDecision(path="direct", reason="公开信息咨询")
        from .intake import request_manifest
        kinds = {g["kind"] for g in request_manifest(message)}
        if kinds.intersection({"transfer", "card_apply", "membership"}):
            return RouteDecision(path="direct", reason="逐项规划明确办理与附加需求")
        if self.research.should_research(message):
            return RouteDecision(path="research", reason="需要核查流水、资金方案或产品证据")
        if re.search(r"转账|转给|(?:给|向).{0,18}(?:转|汇|打)|挂失|解挂|限额|额度|申购|赎回|"
                     r"取消|退订|代扣|会员|查.*(?:余额|账单|卡)|AA|预留|预约|收款", message, re.I):
            return RouteDecision(path="direct", reason="明确办理或简单查询")
        if self.settings.model_mode != "llm":
            return RouteDecision(path="direct", reason="演示模式由业务入口澄清")
        facts = await asyncio.to_thread(self.banking.planner.bank.facts, user_id)
        context = await asyncio.to_thread(self.banking.planner.context_builder.read,
                                         user_id, message, conversation_id, facts=facts)
        try:
            return await self.model.astructured("route", {
                "message": message, "pending_request": context.get("pending_request"),
                "active_plan": {k: context.get("active_plan", {}).get(k) for k in ("id", "status", "summary")},
            }, RouteDecision)
        except BankError:
            return RouteDecision(path="direct", reason="路由暂不可用，进入受约束的业务理解与澄清")

    @task_context
    async def achat(self, user_id, message, conversation_id=None):
        with self.model.harness.scope(user_id, conversation_id or ""):
            return await self._chat_scoped(user_id, message, conversation_id)

    async def _chat_scoped(self, user_id, message, conversation_id=None):
        from .intake import RequestIntake
        intake = getattr(self, 'intake', None) or RequestIntake(self.banking, self.research, self)
        return await intake.chat(user_id, message, conversation_id)

    async def dispatch(self, user_id, message, conversation_id=None):
        decision = await self.decide(user_id, message, conversation_id)
        def log():
            with self.banking.planner.db.write() as session:
                audit(session, user_id, "SUPERVISOR_ROUTED", {
                    "conversation_id": conversation_id, "path": decision.path, "reason": decision.reason,
                })
        await asyncio.to_thread(log)
        if decision.path == "research":
            return await self.research.astart(user_id, ResearchRequest(query=message, conversation_id=conversation_id))
        return await self.banking.astart(user_id, message, conversation_id)

    async def aresearch(self, user_id, request):
        return await self.research.astart(user_id, request)

    def capabilities(self):
        return {
            "architecture": "supervisor_four_specialists", "agents": self.roles,
            "agent_count": len(self.roles),
            "business_services": list(self.banking.planner.services),
            "permissions": {
                "bills_research": "read_only", "wealth_research": "read_only", "verifier": "read_only",
                "business": "draft_and_gateway_submission", "supervisor": "coordinate_without_financial_writes",
            },
        }
