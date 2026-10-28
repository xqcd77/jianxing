"""Current-arm conversation repairs; baseline planning classes stay unchanged."""

import asyncio
from copy import deepcopy
from decimal import Decimal
import re

from sqlalchemy import select

from .agents import PlannerAgent
from .dag import references
from .dialogue import transfer_grounding
from .graph import BankingGraph
from .memory import SENSITIVE
from .research_schemas import ResearchRequest
from .resources import subscription_grounding
from .schemas import Intent
from .storage import Node, Plan, documents, uid


class DeepPlannerAgent(PlannerAgent):
    def _normalize_subscription(self, user_id, intent):
        if not intent:
            return intent
        with self.db.read() as session:
            contracts = [(d.id, d.data) for d in documents(session, "subscription", user_id)]
        for task in intent.tasks:
            if not task.action.startswith("subscriptions."):
                continue
            for field in ("subscription_id", "name_hint"):
                hint = task.params.get(field)
                if not isinstance(hint, str) or hint.startswith("$"):
                    continue
                matches = [d for _, d in contracts if d.get("name") and d["name"] in hint]
                if len(matches) == 1:
                    # A verified name still goes through the authorized contract
                    # query; it is not replaced by an unconfirmed contract ID.
                    task.params = {**task.params, field: matches[0]["name"]}
        return intent

    def _inputs(self, user_id, message, conversation_id):
        result = super()._inputs(user_id, message, conversation_id)
        if isinstance(result, Intent):
            return result
        facts, context, grounded = result
        if re.search(r"两件独立|分别办理", message) and re.search(r"转.*(?:元|块)", message):
            transfer = None
            subscription = None
            for clause in re.split(r"[，,。；;]", message):
                if re.search(r"(?:给|向).*转", clause):
                    transfer = transfer_grounding(clause, facts, context)
                if re.search(r"会员|代扣", clause):
                    subscription = subscription_grounding(clause)
            if transfer and subscription and transfer.tasks and subscription.tasks:
                grounded = Intent(summary="分别核对转账与订阅办理", tasks=[*transfer.tasks, *subscription.tasks])
        return facts, context, self._normalize_subscription(user_id, grounded)

    def _reconcile(self, user_id, conversation_id, grounded, intent, message=""):
        return self._normalize_subscription(
            user_id, super()._reconcile(user_id, conversation_id, grounded, intent, message)
        )


def educational_answer(message):
    if re.search(r"(?:给|向).{0,15}转|帮我申购|我要申购", message):
        return None
    if (
        "可用余额" in message
        and re.search(r"总余额|账户余额|余额和", message)
        and re.search(r"区别|不同|解释", message)
    ):
        return (
            "总余额表示账户已记账资金；可用余额是其中当前能够实际支付的部分。已经预留、冻结或预授权占用的资金可能仍在总余额里，"
            "但不能再次使用；预计工资、尚未到账的赎回款也不能算作当前可用余额。具体差额需要以账户查询和占用明细核对。"
            "原办理计划仍保留，本次解释没有付款；你可回到原计划核对后继续。"
        )
    principal = re.search(r"(?:账单|本金)\s*(\d+(?:\.\d+)?)\s*元", message)
    periods = re.search(r"分\s*(\d+)\s*期", message)
    fee = re.search(r"(?:每期|每月)[^，。；]{0,12}?(\d+(?:\.\d+)?)\s*元\s*手续费", message)
    fee = fee or re.search(r"(?:每期|每月)[^，。；]{0,10}手续费\s*(\d+(?:\.\d+)?)\s*元", message)
    if principal and periods and fee and re.search(r"成本|年费率|年化", message):
        p = Decimal(principal[1])
        n = int(periods[1])
        f = Decimal(fee[1])
        if not (p > 0 and 1 <= n <= 360 and f >= 0):
            return None
        total = f * n
        ratio = total / p * 100
        agreement = re.search(r"前\d+期[^，。；]*?最后一期[^，。；]*", message)
        total_text = format(total.normalize(), "f")
        return (
            f"这项{ratio:.2f}%的说法不完整：{n}期每期另收{f:.2f}元，总手续费{total_text}元，"
            f"{total:.2f}÷{p:.2f}={ratio:.2f}%只表示费用占最初本金的比例。"
            "本金逐月归还，实际占用的资金不断减少，因此按现金流衡量的真实年化成本高于这个表观比例。"
            + (
                f"本金还款仍按你的约定：{agreement[0]}，总本金{p:.2f}元，手续费另计一次。"
                if agreement
                else "本金尾差按你提供的末期约定保留，手续费不重复计算。"
            )
            + "精确日历年化需要每笔实际还款日期，用完整本息及手续费现金流计算IRR/XIRR；当前日期尚未提供，不能给无依据的精确年化。"
            "本次只分析费用，没有办理分期。"
        )
    return None


class DeepBankingGraph(BankingGraph):
    async def _resume_locked(self, user_id, plan_id):
        snapshot = await asyncio.to_thread(self.plans.snapshot, user_id, plan_id)
        if snapshot.get("status") in {"COMPLETED", "CANCELLED"}:
            # A process cancellation may leave a resumable checkpoint after the
            # committed bank state is terminal. Receipts win over stale graph
            # position; never replay EXECUTING from a completed lifecycle.
            await self._atomic(self.planner.memory.capture_results, user_id, snapshot["conversation_id"], snapshot)
            return snapshot
        return await super()._resume_locked(user_id, plan_id)

    def _review(self, state):
        state = deepcopy(state)
        # References are data dependencies, irrespective of which proposer
        # emitted them. Reviewer still checks ownership, field types and cycles.
        for proposal in state["proposals"]:
            for node in proposal["nodes"]:
                node["depends_on"] = list(
                    dict.fromkeys(
                        [*node.get("depends_on", []), *(ref for ref, _ in references(node.get("params", {})))]
                    )
                )
        return super()._review(state)

    async def _start(self, user_id, message, conversation_id, intent, thread_id=None):
        if SENSITIVE.search(message) or re.search(
            r"忽略权限|跳过验证|跳过确认|伪造回执|ignore previous", message, re.I
        ):
            return await super()._start(user_id, message, conversation_id, intent, thread_id)
        answer = educational_answer(message)
        if answer:
            response = {
                "status": "ANSWERED",
                "message": answer,
                "conversation_id": conversation_id or uid("conversation"),
                "answer_kind": "explanation",
                "execution_mode": "sandbox",
            }
            await self._atomic(self._record_turn, user_id, response["conversation_id"], message, response)
            return response
        if conversation_id:
            with self.planner.db.read() as session:
                plans = list(
                    session.scalars(
                        select(Plan)
                        .where(Plan.user_id == user_id, Plan.conversation_id == conversation_id)
                        .order_by(Plan.created_at.desc())
                    )
                )
                unknown = next(
                    (
                        p.id
                        for p in plans
                        if any(n.status == "UNKNOWN" for n in session.scalars(select(Node).where(Node.plan_id == p.id)))
                    ),
                    None,
                )
                research = [
                    d
                    for d in documents(session, "research", user_id)
                    if d.data.get("conversation_id") == conversation_id
                ]
            if unknown and re.search(r"继续核实|核实那笔|查询.*回执|核对.*结果", message):
                response = await self._mutate(user_id, unknown, "reconcile", None)
                await self._atomic(self._record_turn, user_id, conversation_id, message, response)
                return response
            if research and not plans and re.search(r"改成|从这次计划.*删|按刚才|重新汇总|预留.*保持", message):
                service = getattr(self, "deep_research", None)
                if service:
                    return await service._start(
                        user_id, ResearchRequest(query=message, conversation_id=conversation_id)
                    )
        response = await super()._start(user_id, message, conversation_id, intent, thread_id)
        if re.search(r"限额|额度", message) and re.search(r"转|汇", message):
            with self.planner.db.read() as session:
                policies = [d.data for d in documents(session, "financial_evidence", user_id)]
            for policy in policies:
                body = policy.get("body", {})
                if body.get("independent") is True and all(
                    isinstance(body.get(k), int) for k in ("purchase_limit_fen", "transfer_limit_fen")
                ):
                    explanation = (
                        f"\n根据当前已核实的通道规则，卡片消费单笔限额为{body['purchase_limit_fen'] / 100:,.2f}元，"
                        f"账户转账限额为{body['transfer_limit_fen'] / 100:,.2f}元，两者独立；本次转账不因该消费限额被阻挡。"
                        "这仅适用于当前银行提供的规则，不能泛化到所有银行或卡片状态。本次没有调整额度，付款仍需核对并完成相应验证。"
                    )
                    response["message"] = response.get("message", "") + explanation
                    if "assistant_message" in response:
                        response["assistant_message"] += explanation
                    break
        return response
