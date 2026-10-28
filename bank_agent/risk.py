"""Deterministic risk explanations. These views never issue an execution grant."""

from datetime import timedelta

from .contracts import RiskDecision
from .errors import BankError
from .storage import Document
from .product_disclosures import disclosure_clauses

DISCLOSURE_FIELDS = ("risk_level", "minimum_fen", "lock_days", "redeem_business_days", "fee_bps")
FINISHED = {"SUCCEEDED", "PARTIAL", "SKIPPED", "CANCELLED"}


def product_disclosure(session, product_id):
    catalog = session.get(Document, product_id)
    terms = session.get(Document, "disclosure_" + product_id)
    if not catalog or catalog.kind != "product" or catalog.owner_id != "system":
        raise BankError("PRODUCT_TERMS_UNVERIFIED", "产品资料尚未核实，请先查看产品说明后重新选择")
    p = catalog.data
    verified = bool(terms and terms.kind == "product_disclosure" and terms.owner_id == "system"
        and terms.data.get("product_id") == product_id
        and all(type(p.get(k)) is int and terms.data.get("fields", {}).get(k) == p[k] for k in DISCLOSURE_FIELDS)
        and terms.data.get("clauses"))
    if verified:
        disclosed = terms.data.get("fields", {})
        expected = disclosure_clauses({**p, "nav_1e4": disclosed.get("nav_1e4", p["nav_1e4"])})
        verified = terms.data["clauses"] == expected
    if not verified:
        raise BankError("PRODUCT_TERMS_UNVERIFIED", "产品条款缺失或与目录不一致，本次暂不能申购；请核实资料或选择其他产品")
    return p, terms.data


def cashflow_pressure(before, steps, obligations, clock, complete):
    """Replay each dated cash/reserve delta once; no forecast income becomes cash.

    Never subtract obligations from a final simulated balance: that would charge
    subscriptions twice when a scheduled node already advanced the sandbox date.
    """
    end = clock.date() + timedelta(days=30)
    events = []
    cancellations = [s for s in steps if s["operation"] == "subscriptions.cancel" and not s.get("blocked")]
    contract_pending = False
    for step in steps:
        if step.get("blocked"):
            continue
        day = step["scheduled_on"]
        if day > end.isoformat():
            continue
        delta = step["inflow_fen"] - step["outflow_fen"] - step["reserve_change_fen"]
        if delta:
            events.append({"date": day, "label": "待办理资金安排", "delta_fen": delta, "source": step["node_id"]})
    seen = set()
    for item in obligations["obligations"]:
        key = (item["source"], item["date"])
        if key in seen:
            continue
        seen.add(key)
        changes = [s for s in cancellations if s["params"].get("subscription_id") == item["source"]
                   and s["scheduled_on"] <= item["date"]]
        if any(s["params"].get("scope", "both") in {"merchant", "both"} for s in changes):
            continue
        label = item.get("name", "已登记代扣")
        if changes:
            label += " · 停扣后合同支出待核实"
            contract_pending = True
        events.append({"date": item["date"], "label": label,
                       "delta_fen": -item["amount_fen"], "source": item["source"]})
    # Preserve business-DAG order within a date, so releases/receipts stay before
    # their dependent spending. Same-day obligations remain conservatively last.
    events.sort(key=lambda e: e["date"])
    balance = minimum = before["available_fen"]
    shortfall_on = clock.date().isoformat() if balance < 0 else None
    for event in events:
        balance += event["delta_fen"]
        minimum = min(minimum, balance)
        event["projected_available_fen"] = balance
        if balance < 0 and shortfall_on is None:
            shortfall_on = event["date"]
    return {"horizon_end": end.isoformat(), "minimum_available_fen": minimum,
            "shortfall_fen": max(0, -minimum), "shortfall_on": shortfall_on,
            "complete": complete and not contract_pending, "events": events,
            "assumptions": ["覆盖未来30天的本计划及已登记银行代扣，未包含账外支出。",
                "已有预留已从起始可用中扣除；支出与释放预留按计划分别计算。",
                "本情景不计尚未入账的工资与待到账赎回，不代表完整个人财务预测。",
                "测算不预留或扣款；预约及实际提交时仍会重新核验。"]}


class RiskPolicyService:
    """Reuse authoritative tiers, blockers and receipts, never infer fraud from size."""

    @staticmethod
    def describe_plan(session, snapshot):
        preview = snapshot["funding_preview"]
        steps = {s["node_id"]: s for s in preview["steps"]}
        blockers = {b["node_id"]: b for b in preview["blockers"]}
        unknown = any(n["status"] == "UNKNOWN" for n in snapshot["nodes"])
        for node in snapshot["nodes"]:
            step = steps.get(node["id"], {})
            tier = step.get("tier", node["tier"])
            if tier == "UNKNOWN":
                tier = node["tier"]
            terminal = node["status"] in FINISHED
            status = "FINISHED" if terminal else "READ_ONLY" if tier == "GREEN" else "VERIFY" if tier == "RED" else "CONFIRM"
            title = {"FINISHED": "查看办理状态与凭证", "READ_ONLY": "查询与分析", "VERIFY": "敏感操作，请核对并验证", "CONFIRM": "请核对本次操作"}[status]
            reasons, consequences, controls, actions = [], [], [], []

            def reason(code, message, sources=None):
                reasons.append({"code": code, "message": message, "sources": sources or ["当前银行规则", "本次办理计划"]})

            if not terminal:
                if tier == "RED":
                    reason("STRONG_VERIFICATION", "为保护本次敏感操作，请核对详情并验证身份。")
                    controls.append("验证绑定本次对象、金额和计划版本；修改后原授权失效。")
                elif tier == "YELLOW":
                    reason("EXPLICIT_CONFIRMATION", "本步骤会改变资金安排、设置或合同状态，需要你明确确认。")
                elif tier == "GREEN":
                    reason("READ_ONLY", "本步骤只查询或分析，不直接产生资金扣款。")
                if node["status"] in {"READY", "WAIT_TIME", "WAIT_INPUT", "WAIT_HUMAN"}:
                    status, title = "WAITING", "等待前置条件或办理时间"
            if node["operation"] == "funds.transfer":
                consequences.append("实际转出后，取消剩余计划不会撤回已完成的转账。")
                if tier == "RED":
                    reason("DAILY_PAYMENT_TIER", "按当前日累计支付与本次金额核验，超过演示1000元分级阈值，需要强验证。")
            if node["operation"] == "subscriptions.cancel":
                scope = node["params"].get("scope", "both")
                consequences.append("停止银行代扣不等于解除商户合同，也不会自动退款。" if scope == "bank"
                    else "本次核对所选合同与代扣范围；取消不自动产生退款，结果以分别返回的状态为准。")
            if node["operation"] in {"cards.lost", "cards.restore", "cards.unlock", "cards.restrict", "cards.limit"}:
                consequences.append({"cards.lost": "挂失后卡片支付将受限；解挂需要重新核验。",
                    "cards.restore": "解挂会恢复卡片状态，仍受余额、支付限额和其他限制约束。",
                    "cards.unlock": "解除交易限制不等于解除挂失；后续支付仍受银行规则约束。",
                    "cards.restrict": "本次调整支付限额与类别限制，不等于调整信用额度。",
                    "cards.limit": "本次额度申请按银行规则审批，不保证获批。"}[node["operation"]])
            product = None
            if node["operation"] in {"wealth.purchase", "wealth.redeem"}:
                key = node["params"].get("product_id")
                doc = session.get(Document, key) if isinstance(key, str) and not key.startswith("$") else None
                if doc and doc.kind == "product" and doc.owner_id == "system":
                    p = doc.data
                    verified, version = True, None
                    try:
                        _, terms = product_disclosure(session, doc.id)
                        version = terms.get("version")
                    except BankError:
                        verified = False
                    product = {"name": p["name"], "risk_level": p["risk_level"], "minimum_fen": p["minimum_fen"],
                        "lock_days": p["lock_days"], "redeem_business_days": p["redeem_business_days"],
                        "disclosure_verified": verified, "source_version": version,
                        "notes": ["产品存在本金损失可能，不保证收益。", "验证身份不能降低产品投资风险。"]}
                    consequences.append("请同时核对锁定期与赎回到账时间；提交赎回不等于资金已可用。")
            problem = None if terminal else blockers.get(node["id"]) or node["error"]
            if problem:
                status, title = "BLOCKED", "办理条件暂不满足"
                reason(problem["code"], problem["message"], ["执行前条件核验"])
                if problem.get("next_step"):
                    controls.append(problem["next_step"])
                actions = ["edit", "refresh"]
                needs_funding = problem["code"] in {"INSUFFICIENT_AVAILABLE", "PLAN_FUNDING_SHORTFALL"}
                if problem["code"] == "PURCHASE_CONSTRAINT":
                    needs_funding = node["params"].get("amount_fen", 0) > preview["before"]["available_fen"]
                if needs_funding:
                    actions.insert(0, "fund")
                if problem["code"] in {"ASSESSMENT_EXPIRED", "RISK_MISMATCH"}:
                    actions.insert(0, "assess")
                if problem.get("category") == "security":
                    title, actions = "请核实本次安全提示", ["security", "support"]
                if node["status"] == "WAIT_HUMAN":
                    actions.append("support")
            if not terminal and snapshot["security_locked"] and tier != "GREEN":
                status, title, actions = "BLOCKED", "敏感操作已安全暂停", ["security"]
                reason("SECURITY_LOCKED", "请先在安全中心核验身份，解除后返回原业务重新核对。")
            if not terminal and unknown:
                status, title, actions = "VERIFYING", "原交易结果正在核实", ["reconcile"]
                reason("RESULT_UNKNOWN", "银行可能已经受理请求。先核实原交易结果，不能重复付款。", ["银行适配层提交状态"])
            node["risk"] = RiskDecision(authorization_tier=tier, status=status, title=title,
                reasons=reasons, consequences=consequences, controls=controls,
                permitted_next_actions=actions, evaluated_at=preview["business_time"],
                plan_version=snapshot["version"], fingerprint=node["fingerprint"], facts_digest=preview["fingerprint"],
                product=product).model_dump(mode="json")
