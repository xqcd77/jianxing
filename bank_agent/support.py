"""Owned support cases; review proposes a path, the banking gateway executes it."""

from datetime import datetime, timedelta, timezone
import hashlib

from .bank_rules import owned_document
from .errors import BankError
from .memory import SENSITIVE
from .resources import contract_version
from .schemas import Intent
from .storage import Document, User, audit, documents, now_iso, save_document


ACTIVE = {"PENDING", "ACTION_REQUIRED", "FOLLOW_UP"}


def public_case(value):
    return {
        key: value.get(key)
        for key in (
            "id",
            "plan_id",
            "node_id",
            "status",
            "reason_code",
            "title",
            "message",
            "next_step",
            "created_at",
            "updated_at",
            "followup_plan_id",
            "timeline",
        )
    }


def add_event(value, text):
    return {
        **value,
        "updated_at": now_iso(),
        "timeline": [*value.get("timeline", []), {"at": now_iso(), "text": text}][-50:],
    }


def ensure_case(session, user_id, plan, node=None, code="USER_REQUESTED", params=None):
    key = "support_" + hashlib.sha256(f"{plan.id}:{node.id if node else 'plan'}:{code}".encode()).hexdigest()[:28]
    existing = session.get(Document, key)
    if existing:
        return existing.data
    params = params or {}
    merchant = code in {"MERCHANT_UNSUPPORTED", "MERCHANT_CANCELLATION_REQUIRED"}
    if merchant:
        owned_document(session, params.get("subscription_id", ""), "subscription", user_id)
    value = {
        "id": key,
        "plan_id": plan.id,
        "node_id": node.id if node else None,
        "plan_version": plan.version,
        "status": "PENDING",
        "reason_code": code,
        "title": "商户合同取消协助" if merchant else "银行业务协助",
        "message": "银行代扣与商户合同分别处理，核查后只继续未完成部分。"
        if merchant
        else "工作人员核查后给出下一步；资金操作仍由你确认。",
        "next_step": "查看处理记录，补充信息；演示账号可以模拟客服核查。",
        "subscription_id": params.get("subscription_id") if merchant else None,
        "scope": params.get("scope") if merchant else None,
        "review_version": 0,
        "created_at": now_iso(),
        "followup_plan_id": None,
        "timeline": [{"at": now_iso(), "text": "已登记协助请求，尚未联系真实工作人员或商户。"}],
    }
    save_document(session, key, "support_case", user_id, value)
    audit(session, user_id, "SUPPORT_CASE_CREATED", {"case_id": key, "reason_code": code}, plan.id)
    return value


def finish_cases(session, plan):
    for doc in documents(session, "support_case", plan.user_id):
        value = doc.data
        if value["status"] not in ACTIVE:
            continue
        target = value.get("followup_plan_id") or (value["plan_id"] if not value.get("subscription_id") else None)
        if target != plan.id:
            continue
        if plan.status in {"COMPLETED", "CANCELLED"}:
            status = "RESOLVED" if plan.status == "COMPLETED" else "CLOSED"
            text = (
                "关联业务已通过银行网关完成，可查看办理凭证。"
                if status == "RESOLVED"
                else "用户已取消后续安排；已完成的操作保留。"
            )
            doc.data = add_event({**value, "status": status, "next_step": text}, text)
            audit(session, plan.user_id, "SUPPORT_CASE_FINISHED", {"case_id": doc.id, "status": status}, plan.id)


def validate_assistance(session, user_id, params, contract):
    case = owned_document(session, params["assistance_case_id"], "support_case", user_id)
    value = case.data
    if (
        value["status"] not in {"ACTION_REQUIRED", "FOLLOW_UP"}
        or not value.get("verified_proposal")
        or value.get("subscription_id") != contract.id
        or params["scope"] != "merchant"
        or params.get("assistance_version") != value["review_version"]
    ):
        raise BankError("SUPPORT_SCOPE_MISMATCH", "协助方案与本次合同或取消范围不一致，请重新核查", 409)
    if datetime.fromisoformat(value["proposal_expires_at"]) <= datetime.now(timezone.utc):
        raise BankError("SUPPORT_REVIEW_EXPIRED", "协助核查已过期，请先更新工单，再确认办理", 409)
    if value["verified_contract_version"] != contract_version(contract.data):
        raise BankError("CONTRACT_CHANGED", "合同条件已变化，请更新工单并重新核对", 409)
    return case


class SupportService:
    def __init__(self, db, plans, graph, settings):
        self.db, self.plans, self.graph, self.settings = db, plans, graph, settings

    def _owned(self, session, user_id, key):
        return owned_document(session, key, "support_case", user_id)

    def get(self, user_id, key):
        with self.db.read() as session:
            return public_case(self._owned(session, user_id, key).data)

    def list(self, user_id):
        with self.db.read() as session:
            return [
                public_case(d.data)
                for d in sorted(
                    documents(session, "support_case", user_id),
                    key=lambda d: d.data.get("updated_at", d.data["created_at"]),
                    reverse=True,
                )[:50]
            ]

    def close(self, user_id, key):
        with self.db.write() as session:
            doc = self._owned(session, user_id, key)
            if doc.data["status"] in ACTIVE:
                doc.data = add_event(
                    {
                        **doc.data,
                        "status": "CLOSED",
                        "next_step": "协助已关闭；已完成的银行操作保留，原计划可在任务记录中查看。",
                    },
                    "用户关闭协助请求，没有撤销或退款任何已完成交易。",
                )
                audit(session, user_id, "SUPPORT_CASE_CLOSED", {"case_id": key})

    def reopen(self, user_id, key):
        with self.db.write() as session:
            doc = self._owned(session, user_id, key)
            if doc.data["status"] != "CLOSED":
                raise BankError("SUPPORT_NOT_CLOSED", "仅已关闭的协助请求可以重新申请", 409)
            parent = self.plans.owned_plan(session, doc.data["plan_id"], user_id)
            if parent.status in {"CANCELLED", "COMPLETED"}:
                raise BankError("SUPPORT_PLAN_ENDED", "原业务已经结束，请重新提出新的需求", 409)
            doc.data = add_event(
                {
                    **doc.data,
                    "status": "PENDING",
                    "followup_plan_id": None,
                    "verified_proposal": False,
                    "review_version": doc.data["review_version"] + 1,
                    "plan_version": parent.version,
                    "next_step": "已重新登记，请先完成核查再继续办理。",
                },
                "用户重新申请核查，旧协助方案失效。",
            )
            audit(session, user_id, "SUPPORT_CASE_REOPENED", {"case_id": key}, parent.id)

    def message(self, user_id, key, text):
        if SENSITIVE.search(text):
            raise BankError("SENSITIVE_SUPPORT_INPUT", "工单中不要填写密码、验证码或密钥，请使用安全中心")
        with self.db.write() as session:
            doc = self._owned(session, user_id, key)
            if doc.data["status"] not in ACTIVE:
                raise BankError("SUPPORT_CASE_CLOSED", "该工单已结束，不能继续补充", 409)
            doc.data = add_event(doc.data, "用户补充：" + text)
            audit(session, user_id, "SUPPORT_MESSAGE_ADDED", {"case_id": key})
            return doc.data

    def demo_review(self, user_id, key):
        with self.db.write() as session:
            doc = self._owned(session, user_id, key)
            if not self.settings.demo_enabled or not session.get(User, user_id).is_demo_admin:
                raise BankError("DEMO_ONLY", "仅演示账号可以模拟客服核查", 403)
            value = doc.data
            if value["status"] not in ACTIVE:
                return value
            if value.get("followup_plan_id"):
                child = self.plans.owned_plan(session, value["followup_plan_id"], user_id)
                if child.status not in {"FAILED", "CANCELLED", "WAIT_INPUT", "WAIT_HUMAN"}:
                    stale = False
                    if value.get("subscription_id"):
                        current = owned_document(session, value["subscription_id"], "subscription", user_id)
                        stale = value["verified_contract_version"] != contract_version(
                            current.data
                        ) or datetime.fromisoformat(value["proposal_expires_at"]) <= datetime.now(timezone.utc)
                    if not stale:
                        return value
            plan = self.plans.owned_plan(session, value["plan_id"], user_id)
            if plan.version != value["plan_version"]:
                raise BankError("SUPPORT_PLAN_CHANGED", "原安排已经修改，请从最新计划重新申请协助", 409)
            proposal = {}
            if value["reason_code"] == "CHANNEL_CIRCUIT_OPEN":
                check = getattr(self.plans.adapter, "healthcheck", None)
                if not check or not check(session):
                    doc.data = add_event(value, "通道健康检查尚未通过，继续保留工单；没有重发银行操作。")
                    return doc.data
                save_document(session, "circuit_" + user_id, "circuit", user_id, {"failures": 0, "open": False})
                audit(
                    session,
                    user_id,
                    "CHANNEL_HEALTH_RESTORED",
                    {"case_id": key, "source": "sandbox_healthcheck"},
                    plan.id,
                )
            if value.get("subscription_id"):
                contract = owned_document(session, value["subscription_id"], "subscription", user_id)
                if contract.data["merchant_contract"] == "CANCELLED":
                    doc.data = add_event(
                        {**value, "status": "RESOLVED", "next_step": "已核实商户合同为取消状态，未重复办理。"},
                        "核实当前合同已取消，工单结束。",
                    )
                    return doc.data
                proposal = {
                    "verified_proposal": True,
                    "verified_contract_version": contract_version(contract.data),
                    "proposal_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                }
                message = "模拟客服已核实商户可通过协助通道取消。请继续生成商户合同取消步骤，并由你再次确认；银行代扣不会重复操作。"
            else:
                message = "已完成演示核查。请核对原安排，必要时调整信息后继续；风险验证与银行审批不会跳过。"
                if value["reason_code"] == "REPAIR_LIMIT":
                    message = (
                        "原安排多次变化，请先查看原业务，选择“调整安排”重新核对报价或参数，再继续；旧授权不会复用。"
                    )
                if value["reason_code"] == "EVIDENCE_UNAVAILABLE":
                    message = "原核查资料仍缺失，不能当作已验证。请补充可信来源，或重新描述需求以便从当前资料核查；旧业务与工单保留。"
            value = {
                **value,
                **proposal,
                "status": "ACTION_REQUIRED",
                "review_version": value["review_version"] + 1,
                "next_step": message,
                "followup_plan_id": None,
            }
            doc.data = add_event(value, message)
            audit(
                session,
                user_id,
                "SUPPORT_DEMO_REVIEWED",
                {"case_id": key, "source": "sandbox_staff_review", "no_financial_execution": True},
                plan.id,
            )
            return doc.data

    def prepare_followup(self, user_id, key):
        with self.db.read() as session:
            case = self._owned(session, user_id, key).data
            if case["status"] in {"FOLLOW_UP", "RESOLVED"} and case.get("followup_plan_id"):
                return {"existing": case["followup_plan_id"]}
            if case["status"] != "ACTION_REQUIRED":
                raise BankError("SUPPORT_NOT_READY", "请先完成工单核查，再继续办理", 409)
            if case["reason_code"] == "EVIDENCE_UNAVAILABLE":
                raise BankError(
                    "SUPPORT_EVIDENCE_REQUIRED", "缺失资料尚未补齐，请先查看原业务补充可信来源，或重新描述本次需求", 409
                )
            parent = self.plans.owned_plan(session, case["plan_id"], user_id)
            if parent.version != case["plan_version"] or parent.status == "CANCELLED":
                raise BankError("SUPPORT_PLAN_CHANGED", "原安排已修改或取消，请从最新安排发起协助", 409)
            if not case.get("subscription_id"):
                return {"resume": parent.id, "unknown": parent.status == "UNKNOWN"}
            params = {
                "subscription_id": case["subscription_id"],
                "scope": "merchant",
                "assistance_case_id": key,
                "assistance_version": case["review_version"],
                "contract_version": case["verified_contract_version"],
            }
            contract = owned_document(session, case["subscription_id"], "subscription", user_id)
            validate_assistance(session, user_id, params, contract)
            return {
                "params": params,
                "conversation_id": parent.conversation_id,
                "thread_key": f"support_followup:{key}:{case['review_version']}",
            }

    def attach_followup(self, user_id, key, plan_id):
        with self.db.write() as session:
            doc = self._owned(session, user_id, key)
            self.plans.owned_plan(session, plan_id, user_id)
            if doc.data["status"] not in ACTIVE:
                return
            doc.data = add_event(
                {**doc.data, "status": "FOLLOW_UP", "followup_plan_id": plan_id},
                "已生成关联办理计划，等待你核对并按银行要求确认。",
            )
            audit(session, user_id, "SUPPORT_FOLLOWUP_CREATED", {"case_id": key, "followup_plan_id": plan_id})

    async def continue_case(self, user_id, key):
        import asyncio

        async with self.graph.process_locks.ahold("support:" + key):
            action = await asyncio.to_thread(self.prepare_followup, user_id, key)
            if action.get("existing"):
                return await asyncio.to_thread(self.plans.snapshot, user_id, action["existing"])
            if action.get("resume"):
                result = await self.graph.amutate(
                    user_id, action["resume"], "reconcile" if action["unknown"] else "resume"
                )
            else:
                intent = Intent(
                    summary="根据协助核查结果，仅取消商户会员合同",
                    tasks=[{"domain": "subscriptions", "action": "subscriptions.cancel", "params": action["params"]}],
                )
                result = await self.graph.astart(
                    user_id, intent.summary, action["conversation_id"], intent, request_key=action["thread_key"]
                )
            await asyncio.to_thread(self.attach_followup, user_id, key, result["id"])
            return await asyncio.to_thread(self.plans.snapshot, user_id, result["id"])
