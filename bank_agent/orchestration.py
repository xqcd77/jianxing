"""Read-only verification and bounded repair of unexecuted banking tasks."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import select

from .agents import ReviewerAgent
from .errors import BankError
from .operations import business_now, owned_document, risk_tier
from .schemas import NodeSpec, ReplanRequest, StrictModel
from .storage import Document, Node, audit


class Phase(StrEnum):
    COMPILED = "COMPILED"
    VERIFYING = "VERIFYING"
    BACKTRACKING = "BACKTRACKING"
    REPLANNING = "REPLANNING"
    EXECUTING = "EXECUTING"
    AWAITING_INPUT = "AWAITING_INPUT"
    FINISHED = "FINISHED"
    HUMAN = "HUMAN"


PHASE_EDGES = {
    Phase.COMPILED: {Phase.VERIFYING},
    Phase.VERIFYING: {Phase.BACKTRACKING, Phase.EXECUTING, Phase.HUMAN},
    Phase.BACKTRACKING: {Phase.REPLANNING, Phase.VERIFYING, Phase.HUMAN},
    Phase.REPLANNING: {Phase.VERIFYING, Phase.HUMAN},
    Phase.EXECUTING: {Phase.AWAITING_INPUT, Phase.FINISHED, Phase.VERIFYING},
    Phase.AWAITING_INPUT: {Phase.VERIFYING, Phase.FINISHED},
    Phase.FINISHED: {Phase.VERIFYING},  # Explicit API actions still check business lifecycle.
    Phase.HUMAN: {Phase.VERIFYING},
}


class NodeFSM:
    TERMINAL = {"SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"}
    ACTIVE = {"READY", "WAIT_CONFIRM", "WAIT_MFA", "WAIT_TIME", "WAIT_INPUT", "WAIT_HUMAN", "FAILED", "UNKNOWN"}

    @classmethod
    def move(cls, node, status):
        if status not in cls.ACTIVE | cls.TERMINAL:
            raise BankError("INVALID_STATE", "未知业务状态")
        if node.status in cls.TERMINAL and status != node.status:
            raise BankError("TERMINAL_NODE_IMMUTABLE", "已完成或已取消步骤不能重新执行", 409)
        if node.status == "UNKNOWN" and status not in {"UNKNOWN", "SUCCEEDED", "PARTIAL", "READY"}:
            raise BankError("RESULT_UNKNOWN", "原提交仍在核实，不能直接取消或改写", 409)
        # A failed/paused task must explicitly become READY before executing.
        if node.status in {"FAILED", "WAIT_INPUT", "WAIT_HUMAN"} and status in {"SUCCEEDED", "PARTIAL"}:
            raise BankError("INVALID_STATE_TRANSITION", "受阻步骤必须先重新核验", 409)
        node.status = status


class Issue(StrictModel):
    node_id: str
    code: str
    message: str
    repairable: bool = False


class DynamicOrchestrator:
    def __init__(self, plans, memory, settings):
        self.plans, self.memory, self.settings = plans, memory, settings

    def phase(self, user_id, plan_id, phase):
        phase = Phase(phase)
        with self.plans.db.write() as session:
            plan = self.plans.owned_plan(session, plan_id, user_id)
            previous = Phase(plan.review.get("phase", Phase.COMPILED))
            if previous != phase and phase not in PHASE_EDGES[previous]:
                raise BankError("INVALID_PHASE_TRANSITION", "任务阶段迁移无效", 409)
            if previous == phase:
                return
            plan.review = {**plan.review, "phase": phase.value}
            audit(session, user_id, "WORKFLOW_TRANSITION", {"from": previous.value, "to": phase.value}, plan_id)

    def verify(self, user_id, plan_id):
        issues, facts = [], []
        with self.plans.db.read() as session:
            plan = self.plans.owned_plan(session, plan_id, user_id)
            rows = self.plans.ordered_nodes(plan, list(session.scalars(select(Node).where(Node.plan_id == plan_id))))
            specs = [
                NodeSpec(
                    id=n.local_id,
                    agent=n.agent,
                    operation=n.operation,
                    params=n.params,
                    depends_on=n.depends_on,
                    after=plan.review.get("ordering", {}).get(n.local_id, []),
                    scheduled_for=n.scheduled_for,
                )
                for n in rows
            ]
            ReviewerAgent().review(specs)
            nodes = {n.local_id: n for n in rows}
            forbidden = plan.review.get("protected", {}).get("forbidden_operations", [])
            for node in rows:
                if node.status in NodeFSM.TERMINAL or node.status == "UNKNOWN" or plan.status == "CANCELLED":
                    continue
                if node.scheduled_for and datetime.fromisoformat(node.scheduled_for) > business_now(session):
                    continue
                if any(nodes[d].status != "SUCCEEDED" for d in node.depends_on):
                    continue
                if node.operation in forbidden:
                    issues.append(
                        Issue(node_id=node.id, code="USER_CONSTRAINT", message="该步骤与本次明确限制冲突，请调整计划")
                    )
                    continue
                try:
                    params = self.plans._resolve(node.params, nodes)
                    params = self.plans.bank.validate_params(node.operation, params)
                    # prepare(life.purchase) writes quotes; verifier only reads existing source data.
                    if node.operation == "life.purchase" and params.get("quote"):
                        quote = params["quote"]
                        issued = session.get(Document, quote.get("id", ""))
                        if not issued or issued.owner_id != user_id or issued.kind != "quote" or issued.data != quote:
                            raise BankError("UNTRUSTED_QUOTE", "报价来源未通过核验")
                        goal = owned_document(session, params["goal_id"], "goal", user_id)
                        catalog = [session.get(Document, item["id"]) for item in quote.get("items", [])]
                        changed = quote.get("address") != goal.data["address"] or any(
                            not current
                            or any(
                                item.get(k) != current.data.get(k) for k in ["price_fen", "delivery_fen", "merchant_id"]
                            )
                            for item, current in zip(quote.get("items", []), catalog, strict=True)
                        )
                        if changed or quote["expires_at"] < business_now(session).isoformat():
                            issues.append(
                                Issue(
                                    node_id=node.id,
                                    code="QUOTE_CHANGED" if changed else "QUOTE_EXPIRED",
                                    message="报价或配送信息变化，需要重新报价与授权",
                                    repairable=True,
                                )
                            )
                            continue
                    elif node.operation == "funds.transfer":
                        self.plans.bank.prepare(session, user_id, node.operation, params)
                    facts.append(
                        {
                            "node_id": node.id,
                            "risk_tier": risk_tier(session, user_id, node.operation, params),
                            "source": "current_bank_state",
                        }
                    )
                except BankError as error:
                    issues.append(Issue(node_id=node.id, code=error.code, message=error.message))
            report = {
                "plan_version": plan.version,
                "issues": [i.model_dump() for i in issues],
                "facts": facts,
                "repairable": any(i.repairable for i in issues),
                "repair_count": plan.review.get("automatic_repairs", 0),
                "reread_required": plan.review.get("memory", {}).get("reread_required", []),
                "evidence_checked": plan.review.get("evidence_checked", False),
            }
        with self.plans.db.write() as session:
            plan = self.plans.owned_plan(session, plan_id, user_id)
            if plan.version != report["plan_version"]:
                raise BankError("PLAN_VERSION_CONFLICT", "计划已修改，请重新核验", 409)
            for issue in issues:
                if issue.code == "USER_CONSTRAINT":
                    node = session.get(Node, issue.node_id)
                    NodeFSM.move(node, "WAIT_INPUT")
                    node.error = {"code": issue.code, "message": issue.message}
                    node.fingerprint = ""
            plan.review = {**plan.review, "verification": report}
            audit(session, user_id, "BUSINESS_VERIFIED", report, plan_id)
        return report

    def backtrack(self, user_id, plan_id):
        snapshot = self.plans.snapshot(user_id, plan_id)
        report = snapshot["review"].get("verification", {})
        evidence = []
        for record_id in report.get("reread_required", []):
            try:
                original = self.memory.reread(user_id, record_id, conversation_id=snapshot["conversation_id"])
                evidence.append({k: original[k] for k in ["id", "source_id", "source_type", "sha256"]})
            except BankError:
                # Missing/expired evidence cannot be silently treated as verified.
                self.plans.handoff(user_id, plan_id, "EVIDENCE_UNAVAILABLE")
                return {"requires_human": True, "evidence": evidence}
        facts = self.plans.bank.facts(user_id)
        with self.plans.db.write() as session:
            plan = self.plans.owned_plan(session, plan_id, user_id)
            plan.review = {**plan.review, "evidence_checked": True, "backtrack_evidence": evidence}
            audit(
                session,
                user_id,
                "EVIDENCE_REREAD",
                {
                    "evidence": evidence,
                    "bank_state": facts["business_now"],
                    "plan_version": plan.version,
                    "read_only": True,
                },
                plan_id,
            )
        return {"requires_human": False, "evidence": evidence}

    def replan(self, user_id, plan_id):
        snapshot = self.plans.snapshot(user_id, plan_id)
        report = snapshot["review"].get("verification", {})
        repair_ids = {i["node_id"] for i in report.get("issues", []) if i["repairable"]}
        if not repair_ids:
            return snapshot["version"]
        if report.get("repair_count", 0) >= max(0, min(self.settings.max_replans, 5)):
            self.plans.handoff(user_id, plan_id, "REPAIR_LIMIT")
            return snapshot["version"]
        specs = [
            NodeSpec(
                id=n["local_id"],
                agent=n["agent"],
                operation=n["operation"],
                params={**n["params"], "quote": {}} if n["id"] in repair_ids else n["params"],
                depends_on=n["depends_on"],
                after=n["after"],
                scheduled_for=n["scheduled_for"],
                reason=n["reason"],
            )
            for n in snapshot["nodes"]
        ]
        return self.plans.replan(
            user_id,
            plan_id,
            ReplanRequest(
                expected_version=snapshot["version"],
                reason="报价变化，重新取得报价并请求授权",
                nodes=specs,
            ),
            automatic=True,
        )
