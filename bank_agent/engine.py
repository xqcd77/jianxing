"""Persistent business DAG and the only gateway to bank mutations."""

from datetime import datetime
import hashlib

from sqlalchemy import select

from .errors import BankError, ClarificationNeeded
from .dag import references
from .operations import DEFINITIONS, available, business_now, owned_document, resolve_contact, risk_tier
from .security import action_fingerprint
from .presentation import plan_reply
from .storage import Document, Grant, Node, Plan, Receipt, User, audit, canonical, documents, now_iso, save_document, uid
from .adapters import SandboxBankAdapter, SubmissionUncertain
from .failures import error_details
from .locking import ProcessLocks, coordinated
from .support import ensure_case, finish_cases, public_case
from .preflight import PlanSimulator
from .risk import RiskPolicyService
from .request_goals import attach_request_goals

DONE = {"SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"}
WAITS = {"WAIT_CONFIRM", "WAIT_MFA", "WAIT_TIME", "WAIT_INPUT", "WAIT_HUMAN", "FAILED", "UNKNOWN"}


def move_node(node, status):
    from .orchestration import NodeFSM

    NodeFSM.move(node, status)


class PlanService:
    def __init__(self, db, bank, auth):
        self.db, self.bank, self.auth = db, bank, auth
        self.locks = ProcessLocks(db.path.parent)
        self.simulator = PlanSimulator(db, bank)
        self.adapter = SandboxBankAdapter(bank)

    @staticmethod
    def ordered_nodes(plan, nodes):
        order = {key: i for i, key in enumerate(plan.review.get("topological_order", []))}

        def fallback(node):
            import re

            return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", node.local_id)]

        return sorted(nodes, key=lambda node: (order.get(node.local_id, 999), fallback(node)))

    @staticmethod
    def owned_plan(session, plan_id, user_id):
        plan = session.get(Plan, plan_id)
        if not plan or plan.user_id != user_id:
            raise BankError("PLAN_NOT_FOUND", "计划不存在或无权访问", 404)
        return plan

    def create(self, user_id, conversation_id, thread_id, summary, specs, review):
        with self.db.write() as session:
            existing = session.scalar(select(Plan).where(Plan.thread_id == thread_id))
            if existing:
                return existing.id
            plan = Plan(
                id=uid("plan"),
                user_id=user_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                summary=summary,
                status="RUNNING",
                created_at=now_iso(),
                review=review,
            )
            session.add(plan)
            plan.review = {
                **review,
                "protected": {**review.get("protected", {}), "plan_id": plan.id, "plan_version": 1},
            }
            for spec in specs:
                session.add(
                    Node(
                        id=f"{plan.id}_{spec.id}",
                        plan_id=plan.id,
                        local_id=spec.id,
                        agent=spec.agent,
                        operation=spec.operation,
                        params=spec.params,
                        depends_on=spec.depends_on,
                        scheduled_for=spec.scheduled_for,
                        reason=spec.reason,
                        tier=DEFINITIONS[spec.operation][2],
                    )
                )
            audit(
                session,
                user_id,
                "PLAN_REVIEWED",
                {"summary": summary, "review": review, "nodes": [spec.model_dump() for spec in specs]},
                plan.id,
            )
            return plan.id

    @staticmethod
    def _resolve(value, nodes):
        if isinstance(value, str) and value.startswith("$"):
            key, field = next(references(value))
            node = nodes.get(key)
            if not node or node.status != "SUCCEEDED":
                raise BankError("DEPENDENCY_NOT_READY", "依赖任务尚未完成")
            data = node.result["data"]
            for name in field.split("."):
                if not isinstance(data, dict) or name not in data:
                    raise BankError("REFERENCE_NOT_FOUND", "依赖回执没有所需字段")
                data = data[name]
            return data
        if isinstance(value, dict):
            return {key: PlanService._resolve(item, nodes) for key, item in value.items()}
        if isinstance(value, list):
            return [PlanService._resolve(item, nodes) for item in value]
        return value

    def _refresh(self, session, plan, node, nodes):
        params = self._resolve(node.params, nodes)
        params = self.bank.prepare(session, plan.user_id, node.operation, params)
        if plan.review.get("research"):
            from .research_tools import guard_research_purchase
            guard_research_purchase(session, self.bank, plan, node, params)
        tier = risk_tier(session, plan.user_id, node.operation, params)
        recipient = None
        if node.operation == "funds.transfer":
            contact = resolve_contact(session, plan.user_id, params["recipient_hint"])
            recipient = {"id": contact["id"], "account_id": contact["account_id"]}
        fingerprint = action_fingerprint(
            plan.user_id, plan.id, plan.version, node.id, node.operation,
            {"params": params, "tier": tier, "verified_recipient": recipient}
        )
        node.params, node.tier, node.fingerprint = params, tier, fingerprint
        return params, tier, fingerprint

    @coordinated
    def step(self, user_id, plan_id):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            if plan.status in ["COMPLETED", "CANCELLED"]:
                return plan.status
            rows = list(session.scalars(select(Node).where(Node.plan_id == plan.id)))
            rows = self.ordered_nodes(plan, rows)
            nodes = {node.local_id: node for node in rows}
            preview = self.simulator.simulate(session, user_id, rows, plan.review.get("ordering", {}))
            conditions = {b["node_id"]: b for b in preview["blockers"]
                          if not b["future"] and b["category"] == "business" and b["code"] != "DEPENDENCY_NOT_READY"}
            progress = True
            while progress:
                progress = False
                for node in rows:
                    if node.status in DONE or node.status in ["WAIT_INPUT", "WAIT_HUMAN", "FAILED", "UNKNOWN"]:
                        continue
                    deps = [nodes[key] for key in node.depends_on]
                    if any(dep.status in ["CANCELLED", "SKIPPED", "PARTIAL"] for dep in deps):
                        move_node(node, "SKIPPED")
                        node.error = {"code": "DEPENDENCY_CANCELLED", "message": "必要上游未完整完成，此步骤未执行"}
                        progress = True
                        continue
                    if any(dep.status != "SUCCEEDED" for dep in deps):
                        continue
                    ordering = plan.review.get("ordering", {}).get(node.local_id, [])
                    if any(nodes[key].status not in DONE | {"FAILED", "WAIT_INPUT", "WAIT_HUMAN"} for key in ordering):
                        continue
                    if node.scheduled_for and datetime.fromisoformat(node.scheduled_for) > business_now(session):
                        move_node(node, "WAIT_TIME")
                        continue
                    params = {}
                    try:
                        if node.id in conditions:
                            issue = conditions[node.id]
                            if issue["code"] == "CLARIFICATION_REQUIRED":
                                raise ClarificationNeeded(issue["message"])
                            raise BankError(issue["code"], "按当前计划逐步测算：" + issue["message"])
                        circuit = session.get(Document, f"circuit_{user_id}")
                        if circuit and circuit.data.get("open") and node.tier != "GREEN":
                            move_node(node, "WAIT_HUMAN")
                            node.error = {"code": "CHANNEL_CIRCUIT_OPEN", "category": "technical", "message": "业务通道因连续技术故障暂停，请核实服务后再继续", "next_step": "联系工作人员核查业务通道；账户未因此被锁定。"}
                            ensure_case(session, user_id, plan, node, "CHANNEL_CIRCUIT_OPEN")
                            continue
                        params, tier, fingerprint = self._refresh(session, plan, node, nodes)
                        if preview["shortfall_fen"] and node.operation in {"funds.reserve", "funds.transfer", "wealth.purchase", "aa.pay"}:
                            raise BankError("PLAN_FUNDING_SHORTFALL", "整份安排的可用资金不足，请先补款或调整金额，当前步骤尚未付款")
                        if tier != "GREEN" and session.get(User, user_id).locked:
                            move_node(node, "WAIT_INPUT")
                            node.error = {"code": "SECURITY_LOCKED", "category": "security", "message": "账户处于安全锁定状态，本业务尚未执行。", "next_step": "前往安全中心完成验证解锁，再回到此处重新核对；无需转人工。"}
                            continue
                        grant = session.scalar(
                            select(Grant)
                            .where(
                                Grant.node_id == node.id,
                                Grant.user_id == user_id,
                                Grant.fingerprint == fingerprint,
                                Grant.tier == tier,
                                Grant.consumed.is_(False),
                                Grant.expires_at > now_iso(),
                            )
                            .order_by(Grant.id)
                            .limit(1)
                        )
                        if tier != "GREEN" and not grant:
                            move_node(node, "WAIT_MFA" if tier == "RED" else "WAIT_CONFIRM")
                            continue
                        # A savepoint rolls back partial writes on any business exception.
                        with session.begin_nested():
                            if not self.adapter.atomic_local:
                                raise BankError("BANK_TECHNICAL_FAILURE", "网络银行适配器需要独立提交工作进程，当前仅启用事务型沙箱适配器")
                            try:
                                result = self.adapter.submit(session, user_id, node.operation, params, f"node:{node.id}")
                            except SubmissionUncertain:
                                if grant:
                                    grant.consumed = True
                                move_node(node, "UNKNOWN")
                                node.error = {"code": "RESULT_UNKNOWN", "category": "technical", "message": "请求可能已经受理，正在核实银行结果；请勿重复付款。", "next_step": "点击核实结果，先查询原幂等提交的回执。"}
                                save_document(session, "submission_" + node.id, "bank_submission", user_id,
                                    {"node_id": node.id, "plan_id": plan.id, "operation": node.operation, "params": params,
                                     "idempotency_key": "node:" + node.id, "status": "UNKNOWN"})
                                audit(session, user_id, "BANK_SUBMISSION_UNKNOWN", {"node_id": node.id, "idempotency_key": "node:" + node.id}, plan.id)
                                continue
                            stored = session.get(Receipt, result.get("receipt_id")) if isinstance(result, dict) else None
                            if (not stored or stored.owner_id != user_id or stored.operation != node.operation
                                    or stored.idempotency_key != f"node:{node.id}"
                                    or stored.params_hash != hashlib.sha256(canonical(params).encode()).hexdigest()
                                    or stored.result != result):
                                raise BankError("ADAPTER_RESPONSE_INVALID", "银行返回内容与原始凭证不一致，当前步骤未完成", 502)
                            if grant:
                                grant.consumed = True
                            move_node(node, "PARTIAL" if result["data"].get("requires_human") else "SUCCEEDED")
                            node.result, node.error = result, None
                            if result["data"].get("requires_human"):
                                ensure_case(session, user_id, plan, node, "MERCHANT_CANCELLATION_REQUIRED", params)
                            if node.operation == "funds.transfer":
                                save_document(
                                    session,
                                    f"memory_{user_id}_{plan.conversation_id}",
                                    "memory",
                                    user_id,
                                    {"last_recipient": result["data"]["recipient"], "source": result["receipt_id"]},
                                )
                            if tier != "GREEN":
                                save_document(session, f"circuit_{user_id}", "circuit", user_id, {"failures": 0})
                            audit(
                                session,
                                user_id,
                                "OPERATION_EXECUTED",
                                {
                                    "node_id": node.id,
                                    "operation": node.operation,
                                    "tier": tier,
                                    "fingerprint": fingerprint,
                                    "receipt_id": result["receipt_id"],
                                    "result": result["data"],
                                },
                                plan.id,
                            )
                        progress = True
                    except BankError as error:
                        move_node(
                            node,
                            (
                                "WAIT_INPUT"
                                if isinstance(error, ClarificationNeeded)
                                else "WAIT_HUMAN"
                                if error.code == "MERCHANT_UNSUPPORTED"
                                else "FAILED"
                            ),
                        )
                        node.error = error_details(error)
                        if error.code == "MERCHANT_UNSUPPORTED":
                            params = params or self._resolve(node.params, nodes)
                            ensure_case(session, user_id, plan, node, error.code, params)
                        if error.code in ["PRICE_CHANGED", "QUOTE_EXPIRED"]:
                            node.params = {**node.params, "quote": {}}
                            node.fingerprint = ""
                        audit(session, user_id, "OPERATION_BLOCKED", {"node_id": node.id, "error": node.error}, plan.id)
                        if node.error["category"] == "business":
                            audit(session, user_id, "BUSINESS_CONDITION_UNMET", {"node_id": node.id, "code": error.code}, plan.id)
                        else:
                            kind = "suspicion" if node.error["category"] == "security" else "circuit"
                            key = f"{kind}_{user_id}"
                            counter = session.get(Document, key)
                            field = "attempts" if kind == "suspicion" else "failures"
                            value = (counter.data.get(field, 0) if counter else 0) + 1
                            save_document(session, key, kind, user_id, {field: value, "open": value >= 3})
                            if value >= 3:
                                if kind == "suspicion":
                                    session.get(User, user_id).locked = True
                                audit(session, user_id, "SECURITY_LOCKED" if kind == "suspicion" else "CIRCUIT_OPEN", {field: value, "category": node.error["category"]}, plan.id)
                    session.flush()
            statuses = {node.status for node in rows}
            priority = ["UNKNOWN", "WAIT_MFA", "WAIT_CONFIRM", "WAIT_INPUT", "WAIT_HUMAN", "FAILED", "WAIT_TIME"]
            plan.status = next(
                (value for value in priority if value in statuses),
                "PARTIAL" if statuses & {"CANCELLED", "SKIPPED", "PARTIAL"} else "COMPLETED",
            )
            agenda=session.get(Document,'request_goals_'+plan.id)
            if plan.status=='COMPLETED' and agenda and any(g['kind']=='bank_task' and g['status']!='CANCELLED' for g in agenda.data['goals']):
                plan.status='WAIT_INPUT'
            finish_cases(session, plan)
            return plan.status

    @coordinated
    def authorize(self, user_id, plan_id, request):
        valid, error = True, None
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            node = session.get(Node, request.node_id)
            if not node or node.plan_id != plan_id or node.status not in ["WAIT_CONFIRM", "WAIT_MFA"]:
                raise BankError("NOT_AWAITING_APPROVAL", "此节点当前不接受确认", 409)
            previous = node.fingerprint
            rows = {n.local_id: n for n in session.scalars(select(Node).where(Node.plan_id == plan.id))}
            if request.decision == "approve":
                preview = self.simulator.simulate(session, user_id, self.ordered_nodes(plan, list(rows.values())), plan.review.get("ordering", {}))
                if preview["shortfall_fen"] and not (node.scheduled_for and datetime.fromisoformat(node.scheduled_for) > business_now(session)):
                    raise BankError("PLAN_FUNDING_SHORTFALL", "整份安排的可用资金不足，请调低金额或补充资金后再确认", 409)
            _, tier, fingerprint = self._refresh(session, plan, node, rows)
            if previous != fingerprint or request.fingerprint != fingerprint:
                move_node(node, "WAIT_MFA" if tier == "RED" else "WAIT_CONFIRM")
                error = BankError("STALE_APPROVAL", "金额、风险等级或业务详情变化，请刷新后重新确认", 409)
            elif request.decision == "reject":
                move_node(node, "CANCELLED")
                audit(session, user_id, "OPERATION_REJECTED", {"node_id": node.id}, plan.id)
            else:
                user = session.get(User, user_id)
                if user.locked:
                    error = BankError("SECURITY_LOCKED", "敏感操作已锁定，请先解锁", 423)
                elif tier == "RED":
                    valid = self.auth.verify_factor(
                        session, user, request.otp or "", purpose="approve", challenge_id=request.demo_challenge_id,
                        binding={"plan_id": plan.id, "plan_version": plan.version, "node_id": node.id, "fingerprint": fingerprint},
                    )
                    if not valid:
                        error = BankError("INVALID_OTP", "验证码错误、过期或已使用", 401)
                if not error:
                    self.auth.create_grant(session, user_id, node.id, fingerprint, tier)
        # Verification failures must commit their counters before raising.
        if error:
            raise error

    @coordinated
    def snapshot(self, user_id, plan_id):
        with self.db.read() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            nodes = self.ordered_nodes(plan, list(session.scalars(select(Node).where(Node.plan_id == plan_id))))
            snapshot = {
                "id": plan.id,
                "thread_id": plan.thread_id,
                "conversation_id": plan.conversation_id,
                "summary": plan.summary,
                "status": plan.status,
                "version": plan.version,
                "review": plan.review,
                "nodes": [
                    {
                        "id": n.id,
                        "local_id": n.local_id,
                        "agent": n.agent,
                        "operation": n.operation,
                        "params": n.params,
                        "depends_on": n.depends_on,
                        "after": plan.review.get("ordering", {}).get(n.local_id, []),
                        "status": n.status,
                        "tier": n.tier,
                        "scheduled_for": n.scheduled_for,
                        "fingerprint": n.fingerprint,
                        "result": n.result,
                        "error": n.error,
                        "reason": n.reason,
                    }
                    for n in nodes
                ],
                "execution_mode": "sandbox",
                "available_fen": available(session, user_id),
                "security_locked": session.get(User, user_id).locked,
                "support_cases": [public_case(d.data) for d in documents(session, "support_case", user_id)
                    if d.data["plan_id"] == plan.id or d.data.get("followup_plan_id") == plan.id],
            }
            snapshot["assistant_message"] = plan_reply(snapshot)
            if plan.review.get('memory',{}).get('degraded'):
                snapshot['assistant_message'] += '\n' + plan.review['memory']['notice']
            attach_request_goals(session, user_id, snapshot)
        # Preflight writes are ALWAYS rolled back in their own transaction. They
        # cannot expire the ORM objects used to serialize the actual plan above.
        with self.db.simulation() as session:
            p = self.owned_plan(session, plan_id, user_id)
            rows = self.ordered_nodes(p, list(session.scalars(select(Node).where(Node.plan_id == plan_id))))
            preview = self.simulator.simulate(session, user_id, rows, p.review.get("ordering", {}))
            snapshot["funding_preview"] = preview
            snapshot["confirmation_batch"] = self._batch(p, rows, preview)
            RiskPolicyService.describe_plan(session, snapshot)
            from .business_explanations import explain_snapshot
            explain_snapshot(session, user_id, snapshot)
        return snapshot

    def list(self, user_id):
        with self.db.read() as session:
            ids = list(
                session.scalars(
                    select(Plan.id).where(Plan.user_id == user_id).order_by(Plan.created_at.desc()).limit(30)
                )
            )
        return [self.snapshot(user_id, key) for key in ids]

    @staticmethod
    def _batch(plan, rows, preview):
        actual = {n.id: n for n in rows}
        items = []
        for step in preview["steps"]:
            n = actual[step["node_id"]]
            if step["tier"] == "GREEN":
                continue
            if (step["future"] or step["reference_bound"] or step.get("blocked")
                or n.operation == "life.purchase" or step["tier"] != "YELLOW" or n.status not in {"READY", "WAIT_CONFIRM"}):
                break
            items.append({"node_id": n.id, "operation": n.operation,
                          "params": step["params"], "tier": "YELLOW",
                          "display_amount_fen": max(step["outflow_fen"], abs(step["reserve_change_fen"])) or None})
        binding = {"plan_id": plan.id, "version": plan.version,
                   "preflight": preview["fingerprint"], "items": items}
        return {"available": len(items) >= 2 and preview["ready"], "items": items,
                "fingerprint": hashlib.sha256(canonical(binding).encode()).hexdigest(),
                "plan_version": plan.version,
                "message": "仅确认所列黄色步骤，后续敏感或预约操作仍需独立授权。"}

    @coordinated
    def authorize_batch(self, user_id, plan_id, request):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            rows = self.ordered_nodes(plan, list(session.scalars(select(Node).where(Node.plan_id == plan_id))))
            preview = self.simulator.simulate(session, user_id, rows, plan.review.get("ordering", {}))
            batch = self._batch(plan, rows, preview)
            if plan.version != request.expected_version or batch["fingerprint"] != request.manifest_fingerprint:
                raise BankError("STALE_BATCH", "计划或资金条件已变化，请刷新后重新核对整份清单", 409)
            if not batch["available"]:
                raise BankError("BATCH_NOT_AVAILABLE", "当前步骤需分别确认或验证", 409)
            if session.get(User, user_id).locked:
                raise BankError("SECURITY_LOCKED", "敏感操作已锁定，请先解锁", 423)
            by_id = {n.id: n for n in rows}
            local = {n.local_id: n for n in rows}
            for item in batch["items"]:
                n = by_id[item["node_id"]]
                _, tier, fp = self._refresh(session, plan, n, local)
                if tier != "YELLOW":
                    raise BankError("STALE_BATCH", "风险等级已变化，请刷新后重新授权", 409)
                self.auth.create_grant(session, user_id, n.id, fp, tier)
            audit(session, user_id, "YELLOW_BATCH_APPROVED", {"version": plan.version,
                  "manifest_fingerprint": batch["fingerprint"], "items": batch["items"]}, plan_id)

    @coordinated
    def reconcile(self, user_id, plan_id):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            for node in session.scalars(select(Node).where(Node.plan_id == plan_id, Node.status == "UNKNOWN")):
                submission = owned_document(session, "submission_" + node.id, "bank_submission", user_id)
                lookup = self.adapter.lookup(session, user_id, node.operation, submission.data["params"],
                                             submission.data["idempotency_key"])
                if lookup.status == "SUCCEEDED":
                    result = lookup.receipt
                    # The trusted adapter result must bind to the original operation and ledger receipt.
                    if not result or result.get("operation") != node.operation or not result.get("receipt_id"):
                        raise BankError("ADAPTER_RESPONSE_INVALID", "核实回执不完整，仍需人工核查")
                    from .research_tools import digest
                    stored = session.get(Receipt, result["receipt_id"])
                    if (not stored or stored.owner_id != user_id or stored.operation != node.operation
                        or stored.idempotency_key != submission.data["idempotency_key"]
                        or stored.params_hash != digest(submission.data["params"]) or stored.result != result):
                        raise BankError("ADAPTER_RESPONSE_INVALID", "核实结果没有匹配原提交的有效银行回执")
                    node.result, node.error = result, None
                    move_node(node, "PARTIAL" if result["data"].get("requires_human") else "SUCCEEDED")
                    submission.data = {**submission.data, "status": "SUCCEEDED", "receipt_id": result["receipt_id"]}
                    if node.operation == "funds.transfer":
                        save_document(session, f"memory_{user_id}_{plan.conversation_id}", "memory", user_id,
                                      {"last_recipient": result["data"]["recipient"], "source": result["receipt_id"]})
                    audit(session, user_id, "BANK_RESULT_RECONCILED", {"node_id": node.id, "receipt_id": result["receipt_id"]}, plan_id)
                elif lookup.status == "NOT_EXECUTED_FINAL":
                    move_node(node, "READY")
                    node.fingerprint, node.error = "", None
                    submission.data = {**submission.data, "status": "NOT_EXECUTED_FINAL"}
                    audit(session, user_id, "BANK_NON_EXECUTION_CONFIRMED", {"node_id": node.id}, plan_id)
                else:
                    audit(session, user_id, "BANK_RESULT_STILL_UNKNOWN", {"node_id": node.id, "status": lookup.status}, plan_id)

    @staticmethod
    def _require_resolved(session, plan_id):
        if session.scalar(select(Node.id).where(Node.plan_id == plan_id, Node.status == "UNKNOWN").limit(1)):
            raise BankError("RESULT_UNKNOWN", "原提交仍在核实，请先查询结果再修改或取消计划", 409)

    @coordinated
    def edit(self, user_id, plan_id, request):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            if request.expected_version is not None and request.expected_version != plan.version:
                raise BankError("PLAN_VERSION_CONFLICT", "安排已在其他操作中更新，请刷新后重新修改", 409)
            self._require_resolved(session, plan_id)
            node = session.get(Node, request.node_id)
            if not node or node.plan_id != plan_id or node.status in DONE or plan.status == "CANCELLED":
                raise BankError("NOT_EDITABLE", "已执行或已取消节点不能修改", 409)
            if node.operation == "life.purchase" and request.params.get("quote"):
                raise BankError("SERVER_QUOTE_ONLY", "报价必须由商户后端生成")
            before_params = dict(node.params)
            before_schedule = node.scheduled_for
            node.params = self.bank.validate_params(node.operation, request.params)
            if "scheduled_for" in request.model_fields_set:
                if node.operation != "funds.transfer":
                    raise BankError("SCHEDULE_DERIVED", "生日预约时间由目标日期推导，请调整生日目标")
                if request.scheduled_for:
                    try:
                        when = datetime.fromisoformat(request.scheduled_for)
                    except ValueError as error:
                        raise BankError("INVALID_SCHEDULE", "请指定有效的预约时间") from error
                    if when.tzinfo is None or when < business_now(session):
                        raise BankError("INVALID_SCHEDULE", "预约时间需要时区且不能早于当前业务时间")
                node.scheduled_for = request.scheduled_for
            move_node(node, "READY")
            node.error, node.fingerprint = None, ""
            plan.version += 1
            plan.status = "RUNNING"
            plan.review = {**plan.review, "last_revision": {"version": plan.version, "reason": "用户调整办理安排",
                "changes": [{"node_id": node.id, "change": "updated", "before": before_params,
                    "after": node.params, "before_scheduled_for": before_schedule, "after_scheduled_for": node.scheduled_for}]}}
            if node.operation == "life.goal":
                from datetime import date, timedelta

                birthday = date.fromisoformat(node.params["birthday_date"])
                month = birthday.replace(day=1).isoformat() + "T09:00:00+08:00"
                ordering = {key: list(value) for key, value in plan.review.get("ordering", {}).items()}
                for child in session.scalars(select(Node).where(Node.plan_id == plan_id)):
                    if child.local_id == f"{node.local_id}_reserve" and child.status not in DONE:
                        child.params = {**child.params, "amount_fen": node.params["budget_fen"]}
                        child.scheduled_for = month if datetime.fromisoformat(month) > business_now(session) else None
                        move_node(child, "READY")
                        child.fingerprint, child.error = "", None
                        if child.scheduled_for:
                            ordering = {
                                key: [dep for dep in value if dep != child.local_id] for key, value in ordering.items()
                            }
                    if child.local_id == f"{node.local_id}_purchase" and child.status not in DONE:
                        child.scheduled_for = (birthday - timedelta(days=2)).isoformat() + "T09:00:00+08:00"
                        child.params = {**child.params, "card_id": node.params["card_id"], "quote": {}}
                        move_node(child, "READY")
                        child.fingerprint, child.error = "", None
                from .agents import ReviewerAgent
                from .schemas import NodeSpec

                specs = [
                    NodeSpec(
                        id=n.local_id,
                        agent=n.agent,
                        operation=n.operation,
                        params=n.params,
                        depends_on=n.depends_on,
                        after=ordering.get(n.local_id, []),
                        scheduled_for=n.scheduled_for,
                    )
                    for n in session.scalars(select(Node).where(Node.plan_id == plan_id))
                ]
                plan.review = {**plan.review, **ReviewerAgent().review(specs)}
            for grant in session.scalars(
                select(Grant).join(Node, Node.id == Grant.node_id).where(Node.plan_id == plan_id)
            ):
                grant.consumed = True
            from .presentation import revised_plan_title

            changed_rows = self.ordered_nodes(plan, list(session.scalars(select(Node).where(Node.plan_id == plan_id))))
            names = {p.id: p.data["name"] for p in documents(session, "product", "system")}
            plan.summary = revised_plan_title(changed_rows, names)
            audit(
                session,
                user_id,
                "PLAN_EDITED",
                {"node_id": node.id, "version": plan.version, "params": node.params},
                plan_id,
            )

    @coordinated
    def retry(self, user_id, plan_id):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            for node in session.scalars(select(Node).where(Node.plan_id == plan_id)):
                if node.status in ["FAILED", "WAIT_HUMAN", "WAIT_INPUT"]:
                    move_node(node, "READY")
                    node.error = None
            if plan.status not in ["COMPLETED", "CANCELLED"]:
                plan.status = "RUNNING"

    @coordinated
    def replan(self, user_id, plan_id, request, automatic=False, protected=None):
        from .agents import ReviewerAgent, infer_ordering
        from .orchestration import NodeFSM
        from .schemas import NodeSpec

        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            self._require_resolved(session, plan_id)
            if plan.status in {"CANCELLED", "COMPLETED", "PARTIAL"}:
                raise BankError("PLAN_NOT_REPLANNABLE", "已结束的计划不能重新规划", 409)
            if plan.version != request.expected_version:
                raise BankError("PLAN_VERSION_CONFLICT", "计划已修改，请刷新后重新提交", 409)
            rows = list(session.scalars(select(Node).where(Node.plan_id == plan_id)))
            originals = {n.local_id: n for n in rows}
            proposed = {n.id: n for n in request.nodes}
            if len(proposed) != len(request.nodes):
                raise BankError("INVALID_DAG", "计划节点编号重复")

            def spec_of(n):
                return NodeSpec(
                    id=n.local_id,
                    agent=n.agent,
                    operation=n.operation,
                    params=n.params,
                    depends_on=n.depends_on,
                    after=plan.review.get("ordering", {}).get(n.local_id, []),
                    scheduled_for=n.scheduled_for,
                    reason=n.reason,
                )

            for local_id, node in originals.items():
                if node.status in NodeFSM.TERMINAL:
                    expected = spec_of(node)
                    if local_id in proposed and proposed[local_id].model_dump() != expected.model_dump():
                        raise BankError("TERMINAL_NODE_IMMUTABLE", "重规划不能改变已执行节点或原始回执", 409)
                    proposed[local_id] = expected
                elif local_id not in proposed:
                    if automatic:
                        raise BankError("UNSAFE_AUTOMATIC_REPLAN", "自动修复不能删除业务步骤")
                    proposed[local_id] = spec_of(node)
            specs = list(proposed.values())
            if not automatic:
                specs = infer_ordering(specs, {n.local_id for n in rows if n.status in NodeFSM.TERMINAL})
            review = ReviewerAgent().review(specs)
            changes = []
            for spec in specs:
                node = originals.get(spec.id)
                if node and node.status in NodeFSM.TERMINAL:
                    continue
                # Server-issued quote payloads cannot be introduced by a client or model.
                if (
                    spec.operation == "life.purchase"
                    and spec.params.get("quote")
                    and (not node or spec.params["quote"] != node.params.get("quote"))
                ):
                    raise BankError("SERVER_QUOTE_ONLY", "重规划报价必须重新由商户后端生成")
                if not list(references(spec.params)):
                    spec.params = self.bank.validate_params(spec.operation, spec.params)
                if automatic and (
                    not node
                    or spec.operation != node.operation
                    or spec.agent != node.agent
                    or spec.depends_on != node.depends_on
                    or spec.scheduled_for != node.scheduled_for
                    or {k: v for k, v in spec.params.items() if k != "quote"}
                    != {k: v for k, v in node.params.items() if k != "quote"}
                ):
                    raise BankError("UNSAFE_AUTOMATIC_REPLAN", "自动修复只能更新既有采购的报价")
                if not node:
                    node = Node(
                        id=f"{plan_id}_{spec.id}",
                        plan_id=plan_id,
                        local_id=spec.id,
                        agent=spec.agent,
                        operation=spec.operation,
                        params=spec.params,
                        depends_on=spec.depends_on,
                        scheduled_for=spec.scheduled_for,
                        reason=spec.reason,
                        tier=DEFINITIONS[spec.operation][2],
                        status="READY",
                    )
                    session.add(node)
                    changes.append({"node_id": node.id, "change": "added"})
                else:
                    if spec.id not in {s.id for s in request.nodes}:
                        NodeFSM.move(node, "CANCELLED")
                        changes.append({"node_id": node.id, "change": "cancelled"})
                    else:
                        if spec.model_dump() != spec_of(node).model_dump():
                            changes.append(
                                {"node_id": node.id, "change": "updated", "before": node.params, "after": spec.params,
                                    "before_scheduled_for": node.scheduled_for, "after_scheduled_for": spec.scheduled_for}
                            )
                        node.params, node.agent, node.operation = spec.params, spec.agent, spec.operation
                        node.depends_on, node.scheduled_for, node.reason = (
                            spec.depends_on,
                            spec.scheduled_for,
                            spec.reason,
                        )
                        NodeFSM.move(node, "READY")
                    node.error, node.fingerprint = None, ""
            for grant in session.scalars(
                select(Grant).join(Node, Node.id == Grant.node_id).where(Node.plan_id == plan_id)
            ):
                grant.consumed = True
            plan.version += 1
            plan.status = "RUNNING"
            binding = dict(plan.review.get("protected", {}))
            if protected:
                incoming = protected.model_dump(mode="json")
                binding = {
                    **binding,
                    **incoming,
                    "negative_constraints": list(
                        dict.fromkeys(binding.get("negative_constraints", []) + incoming["negative_constraints"])
                    ),
                    "forbidden_operations": list(
                        set(binding.get("forbidden_operations", [])) | set(incoming["forbidden_operations"])
                    ),
                }
            binding.update(plan_id=plan.id, plan_version=plan.version)
            from .presentation import revised_plan_title

            names = {p["id"]: p["name"] for p in self.bank.facts(user_id)["products"]}
            plan.summary = revised_plan_title(specs, names)
            plan.review = {
                **plan.review,
                **review,
                "evidence_checked": False,
                "automatic_repairs": plan.review.get("automatic_repairs", 0) + 1 if automatic else 0,
                "last_revision": {"version": plan.version, "reason": request.reason, "changes": changes},
                "protected": binding,
            }
            audit(
                session,
                user_id,
                "PLAN_REPLANNED",
                {
                    "version": plan.version,
                    "automatic": automatic,
                    "reason": request.reason,
                    "changes": changes,
                    "completed_receipts_preserved": True,
                },
                plan_id,
            )
            return plan.version

    @coordinated
    def cancel_node(self, user_id, plan_id, request):
        """Cancel an unsubmitted goal and its dependent steps; never compensate receipts."""
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            self._require_resolved(session, plan_id)
            if plan.version != request['expected_version']:
                raise BankError('PLAN_VERSION_CONFLICT', '安排已更新，请刷新后重新修改', 409)
            nodes = list(session.scalars(select(Node).where(Node.plan_id == plan_id)))
            target = next((n for n in nodes if n.id == request['node_id']), None)
            if not target or target.status in DONE:
                raise BankError('NOT_EDITABLE', '已结束的步骤不能取消', 409)
            cancelled = {target.local_id}
            while True:
                expanded = cancelled | {n.local_id for n in nodes if set(n.depends_on) & cancelled}
                if expanded == cancelled:
                    break
                cancelled = expanded
            for node in nodes:
                if node.local_id in cancelled and node.status not in DONE:
                    move_node(node, 'CANCELLED')
                    node.fingerprint = ''
            for grant in session.scalars(select(Grant).join(Node, Node.id == Grant.node_id).where(Node.plan_id == plan_id)):
                grant.consumed = True
            plan.version += 1
            plan.status = 'RUNNING'
            audit(session, user_id, 'PLAN_GOAL_CANCELLED', {'node_id': target.id, 'version': plan.version,
                'cancelled_nodes': sorted(cancelled), 'completed_transactions_preserved': True}, plan_id)

    @coordinated
    def cancel(self, user_id, plan_id):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            self._require_resolved(session, plan_id)
            for node in session.scalars(select(Node).where(Node.plan_id == plan_id)):
                if node.status not in DONE:
                    move_node(node, "CANCELLED")
                if node.operation == "funds.reserve" and node.result:
                    hold = owned_document(session, node.result["data"]["hold_id"], "hold", user_id)
                    if hold.data["status"] == "ACTIVE":
                        result = self.bank.execute_once(
                            session, user_id, "funds.release", {"hold_id": hold.id}, f"cancel:{node.id}"
                        )
                        audit(
                            session,
                            user_id,
                            "COMPENSATION_EXECUTED",
                            {"hold_id": hold.id, "receipt_id": result["receipt_id"]},
                            plan_id,
                        )
            for grant in session.scalars(
                select(Grant).join(Node, Node.id == Grant.node_id).where(Node.plan_id == plan_id)
            ):
                grant.consumed = True
            plan.status = "CANCELLED"
            for node in session.scalars(select(Node).where(Node.plan_id == plan_id, Node.operation == "life.goal")):
                if node.result:
                    goal = owned_document(session, node.result["data"]["goal_id"], "goal", user_id)
                    if goal.data["status"] != "COMPLETED":
                        goal.data = {**goal.data, "status": "CANCELLED"}
            audit(session, user_id, "PLAN_CANCELLED", {"completed_transactions_preserved": True}, plan_id)
            finish_cases(session, plan)

    @coordinated
    def handoff(self, user_id, plan_id, reason_code="USER_REQUESTED"):
        with self.db.write() as session:
            plan = self.owned_plan(session, plan_id, user_id)
            if plan.status in {"COMPLETED", "CANCELLED"}:
                raise BankError("PLAN_ALREADY_ENDED", "这项安排已经结束，可查看结果；如有新的需求请重新提出", 409)
            for node in session.scalars(select(Node).where(Node.plan_id == plan_id)):
                if node.status not in DONE and node.status != "UNKNOWN":
                    move_node(node, "WAIT_HUMAN")
            plan.status = "UNKNOWN" if any(n.status == "UNKNOWN" for n in session.scalars(select(Node).where(Node.plan_id == plan_id))) else "WAIT_HUMAN"
            ticket = ensure_case(session, user_id, plan, code=reason_code)
            plan.review = {**plan.review, "human_handoff": ticket}
            for grant in session.scalars(
                select(Grant).join(Node, Node.id == Grant.node_id).where(Node.plan_id == plan_id)
            ):
                grant.consumed = True
            audit(session, user_id, "HUMAN_HANDOFF", {"message": "人工需修正参数后仍经同一授权网关执行"}, plan_id)

    def audit_log(self, user_id):
        from .storage import AuditEvent

        with self.db.read() as session:
            rows = list(session.scalars(select(AuditEvent).order_by(AuditEvent.seq)))
            previous, verified = "0" * 64, True
            for row in rows:
                payload = {
                    "user_id": row.user_id,
                    "kind": row.kind,
                    "plan_id": row.plan_id,
                    "details": row.details,
                    "created_at": row.created_at,
                }
                digest = hashlib.sha256((previous + canonical(payload)).encode()).hexdigest()
                verified &= row.previous_hash == previous and row.event_hash == digest
                previous = row.event_hash
            return {
                "chain_verified": verified,
                "chain_scope": "local_database",
                "events": [
                    {
                        "seq": r.seq,
                        "kind": r.kind,
                        "plan_id": r.plan_id,
                        "details": r.details,
                        "created_at": r.created_at,
                        "event_hash": r.event_hash,
                    }
                    for r in rows
                    if r.user_id == user_id
                ],
            }
