import calendar
from datetime import date, datetime, timedelta, timezone
import hashlib

from sqlalchemy import select

from .errors import BankError
from .operations import available, business_now, primary_account
from .storage import Account, Document, Node, Plan, Receipt, User, audit, canonical, documents, save_document, uid
from .funding import incoming_intent
from .presentation import yuan


class EventService:
    def __init__(self, db, bank, graph, settings):
        self.db, self.bank, self.graph, self.settings = db, bank, graph, settings

    def require_demo_admin(self, session, user_id):
        user = session.get(User, user_id)
        if not self.settings.demo_enabled or not user.is_demo_admin:
            raise BankError("DEMO_ADMIN_REQUIRED", "只有本地演示管理员可以注入模拟事件", 403)

    @staticmethod
    def _blocked_transfer(session, user_id, conversation_id):
        if not conversation_id:
            return None
        dialogue = session.get(Document, f"dialogue_{user_id}_{conversation_id}")
        if not dialogue or dialogue.owner_id != user_id:
            return None
        last_intent = dialogue.data.get("last_intent", {})
        tasks = last_intent.get("tasks", [])
        last_request = next(
            (turn["content"] for turn in reversed(dialogue.data.get("turns", [])) if turn["role"] == "user"), ""
        )
        legacy_funding = incoming_intent(last_request, own_account_context=True)
        # Recover this exact legacy bad case without treating an unrelated refusal
        # as fresh transfer context. The actual failed plan is still checked below.
        rejected_funding = bool(
            not tasks
            and last_intent.get("reject_reason")
            and last_intent.get("rejection_kind", "business") == "business"
            and legacy_funding
            and "amount_fen" in legacy_funding
        )
        if not rejected_funding and (len(tasks) != 1 or tasks[0].get("action") != "funds.transfer"):
            return None
        plan = session.get(Plan, dialogue.data.get("active_plan_id", ""))
        if (
            not plan
            or plan.user_id != user_id
            or plan.conversation_id != conversation_id
            or plan.status in {"COMPLETED", "CANCELLED", "PARTIAL"}
        ):
            return None
        nodes = list(session.scalars(select(Node).where(Node.plan_id == plan.id)))
        if len(nodes) != 1:
            return None
        node = nodes[0]
        if node.status != "FAILED" or (node.error or {}).get("code") != "INSUFFICIENT_AVAILABLE":
            return None
        if not rejected_funding and any(
            tasks[0].get("params", {}).get(k) != node.params[k] for k in ["recipient_hint", "amount_fen"]
        ):
            return None
        return {
            "plan_id": plan.id,
            "version": plan.version,
            "recipient": node.params["recipient_hint"],
            "amount_fen": node.params["amount_fen"],
        }

    def recognize_incoming(self, user_id, conversation_id, text):
        # Relative destinations are grounded in the same user's same conversation,
        # never in a global latest plan or the model's unsupported-business answer.
        with self.db.read() as session:
            blocked = self._blocked_transfer(session, user_id, conversation_id)
            grounded = bool(blocked)
            if conversation_id:
                draft = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
                grounded = grounded or bool(
                    draft
                    and draft.owner_id == user_id
                    and not draft.data.get("clarify_destination")
                    and draft.data["expires_at"] > datetime.now(timezone.utc).isoformat()
                )
            parsed = incoming_intent(text, own_account_context=grounded)
            if parsed and parsed.pop("cover_shortfall", False) and blocked:
                shortfall = max(0, blocked["amount_fen"] - available(session, user_id))
                if shortfall == 0:
                    return {"guidance": "funds_already_sufficient"}
                parsed["amount_fen"] = shortfall
        return parsed or self.pending_incoming_amount(user_id, conversation_id, text)

    def prepare_incoming(self, user_id, amount_fen, conversation_id=None, *, clarify_destination=False):
        """Prepare a UI-only fixture request. The model cannot post this event."""
        conversation_id = conversation_id or uid("conversation")
        with self.db.write() as session:
            user = session.get(User, user_id)
            previous_draft = session.get(Document, f"draft_{user_id}_{conversation_id}")
            if previous_draft:
                session.delete(previous_draft)
            if clarify_destination:
                save_document(
                    session,
                    f"incoming_draft_{user_id}_{conversation_id}",
                    "incoming_draft",
                    user_id,
                    {
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                        "clarify_destination": True,
                        "amount_fen": amount_fen,
                    },
                )
                amount = yuan(amount_fen) if amount_fen is not None else "这笔钱"
                return {
                    "status": "NEEDS_CLARIFICATION",
                    "conversation_id": conversation_id,
                    "message": f"你说的“里面”是指你自己的银行账户吗？转入金额已记为{amount}，确认账户后再安排。"
                    if amount_fen is not None
                    else "你说的“里面”是指你自己的银行账户吗？",
                    "execution_mode": "sandbox",
                }
            if not self.settings.demo_enabled or not user.is_demo_admin:
                return {
                    "status": "INCOMING_GUIDANCE",
                    "conversation_id": conversation_id,
                    "message": "向自己的账户转入资金，需要从转出银行发起转账。当前版本尚未连接外部银行卡，不能替你从其他银行卡扣款。请在转出银行核对本人收款账户后办理。",
                    "execution_mode": "sandbox",
                }
            if amount_fen is None:
                save_document(
                    session,
                    f"incoming_draft_{user_id}_{conversation_id}",
                    "incoming_draft",
                    user_id,
                    {"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()},
                )
                return {
                    "status": "NEEDS_CLARIFICATION",
                    "conversation_id": conversation_id,
                    "message": "你准备向自己的账户转入多少元？当前演示账户可以模拟这笔资金到账。",
                    "execution_mode": "sandbox",
                }
            key = uid("incoming_request")
            data = {
                "amount_fen": amount_fen,
                "state": "PENDING",
                "conversation_id": conversation_id,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            }
            continuation = self._blocked_transfer(session, user_id, conversation_id)
            if continuation:
                data["continuation"] = continuation
            save_document(session, key, "incoming_request", user_id, data)
            draft = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
            if draft:
                session.delete(draft)
            audit(session, user_id, "DEMO_INCOMING_PREPARED", {"request_id": key, "amount_fen": amount_fen})
            message = f"你准备向自己的账户转入{yuan(amount_fen)}。当前为演示账户，可以先模拟这笔资金到账；不会从其他银行卡扣款。请核对下方金额，再确认模拟转入。"
            if continuation:
                message += f"给{continuation['recipient']}转账{yuan(continuation['amount_fen'])}的安排会保留，到账后仍需单独确认。"
            return {
                "status": "WAIT_DEMO_CONFIRM",
                "conversation_id": conversation_id,
                "message": message,
                "execution_mode": "sandbox",
                "ui_action": {"kind": "demo_incoming", "request_id": key, "amount_fen": amount_fen},
            }

    def pending_incoming_amount(self, user_id, conversation_id, text):
        if not conversation_id:
            return None
        with self.db.read() as session:
            draft = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
            if not draft or datetime.fromisoformat(draft.data["expires_at"]) <= datetime.now(timezone.utc):
                return None
        # Only a bare amount can answer this pending question; other business
        # instructions still go to the planner rather than silently becoming money.
        import re
        from .agents import money

        if draft.data.get("clarify_destination"):
            if re.fullmatch(
                r"\s*(?:是(?:的)?|对(?:的)?|没错|自己|本人|(?:是|就是|对[，,]?是|转入|往|给|向)?(?:我(?:自己)?(?:的)?|本人(?:的)?|自己(?:的)?)(?:银行)?(?:账户|账号|银行卡|卡)(?:里|上)?)\s*[。！!]*",
                text,
            ):
                return {"amount_fen": draft.data.get("amount_fen")}
            recipient = re.sub(r"^(?:不是[，,]?|转(?:账)?给|给|向|往)", "", text.strip().rstrip("。！! "))
            contacts = self.bank.facts(user_id)["contacts"]
            matches = [
                c
                for c in contacts
                if c.get("verified") and recipient in [c["name"], c["phone"], *c["aliases"], *c["notes"]]
            ]
            if len(matches) == 1 or re.fullmatch(r"\s*(?:不是|不|给别人|转给别人|转出)\s*[。！!]*", text):
                with self.db.write() as session:
                    abandoned = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
                    if abandoned:
                        session.delete(abandoned)
                return {
                    "outgoing_hint": matches[0]["name"] if len(matches) == 1 else None,
                    "amount_fen": draft.data.get("amount_fen"),
                }
            # A bare amount cannot resolve an unknown destination.
            if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*[万千]?\s*[元块]?\s*", text):
                try:
                    amount = money(text)
                except BankError:
                    return {"invalid_amount": True}
                if not 0 < amount <= 100_000_000:
                    return {"invalid_amount": True}
                return {"amount_fen": amount, "clarify_destination": True}
        if not re.fullmatch(r"\s*\d+(?:\.\d+)?\s*[万千]?\s*[元块]?\s*", text):
            with self.db.write() as session:
                abandoned = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
                if abandoned:
                    session.delete(abandoned)
            return None
        try:
            amount = money(text)
        except BankError:
            return {"invalid_amount": True}
        return {"amount_fen": amount} if 0 < amount <= 100_000_000 else {"invalid_amount": True}

    def clear_incoming_draft(self, user_id, conversation_id):
        if not conversation_id:
            return
        with self.db.write() as session:
            draft = session.get(Document, f"incoming_draft_{user_id}_{conversation_id}")
            if draft and draft.owner_id == user_id:
                session.delete(draft)
                audit(
                    session,
                    user_id,
                    "INCOMING_DRAFT_CLOSED",
                    {"conversation_id": conversation_id, "reason": "new_business"},
                )

    def decide_incoming(self, user_id, request_id, decision):
        with self.db.write() as session:
            self.require_demo_admin(session, user_id)
            request = session.get(Document, request_id)
            if not request or request.kind != "incoming_request" or request.owner_id != user_id:
                raise BankError("NOT_FOUND", "没有找到这笔转入安排", 404)
            key = "incoming:" + user_id + ":" + request_id
            receipt = session.scalar(select(Receipt).where(Receipt.idempotency_key == key))
            if receipt:
                if decision == "cancel":
                    raise BankError("ALREADY_POSTED", "这笔模拟资金已入账，不能再取消到账事件", 409)
                return receipt.result
            if request.data["state"] == "CANCELLED":
                if decision == "cancel":
                    return {"status": "CANCELLED", "message": "已取消这笔模拟转入，账户余额没有变化。"}
                raise BankError("REQUEST_CANCELLED", "这笔模拟转入已取消，请重新提出需求", 409)
            if datetime.fromisoformat(request.data["expires_at"]) <= datetime.now(timezone.utc):
                raise BankError("REQUEST_EXPIRED", "这笔转入确认已过期，请重新提出需求", 409)
            if decision == "cancel":
                request.data = {**request.data, "state": "CANCELLED"}
                audit(session, user_id, "DEMO_INCOMING_CANCELLED", {"request_id": request_id})
                return {"status": "CANCELLED", "message": "已取消这笔模拟转入，账户余额没有变化。"}
            user = session.get(User, user_id)
            if user.locked:
                raise BankError("ACCOUNT_LOCKED", "账户已安全锁定，请先在安全中心解除锁定", 423)
            if not session.get(Account, "sys_incoming"):
                session.add(Account(id="sys_incoming", user_id="system", name="模拟外部转入对手户", system=True))
                session.flush()
            amount = request.data["amount_fen"]
            receipt_id = uid("receipt")
            account = primary_account(session, user_id)
            tx = self.bank._transaction(
                session,
                user_id,
                receipt_id,
                amount,
                "sys_incoming",
                account.id,
                "INCOMING",
                "模拟本人账户转入",
                "income",
            )
            result = {
                "status": "COMPLETED",
                "amount_fen": amount,
                "balance_fen": account.balance_fen,
                "receipt_id": receipt_id,
                "transaction_id": tx,
                "execution_mode": "sandbox",
                "message": f"已模拟转入{yuan(amount)}。当前账户余额为{yuan(account.balance_fen)}。这笔到账仅用于演示。",
            }
            continuation = request.data.get("continuation")
            current = self._blocked_transfer(session, user_id, request.data["conversation_id"])
            if continuation and current == continuation:
                enough = available(session, user_id) >= current["amount_fen"]
                result["continuation"] = {
                    **current,
                    "conversation_id": request.data["conversation_id"],
                    "can_resume": enough,
                }
                if enough:
                    result["message"] += (
                        f"现在可以继续给{current['recipient']}转账{yuan(current['amount_fen'])}；这笔钱尚未转出，仍需单独确认。"
                    )
                else:
                    result["message"] += (
                        f"给{current['recipient']}转账{yuan(current['amount_fen'])}仍受可用余额限制，这笔钱尚未转出。"
                    )
            session.add(
                Receipt(
                    id=receipt_id,
                    owner_id=user_id,
                    operation="event.incoming",
                    idempotency_key=key,
                    params_hash=hashlib.sha256(canonical({"amount_fen": amount}).encode()).hexdigest(),
                    result=result,
                    created_at=business_now(session).isoformat(),
                )
            )
            request.data = {**request.data, "state": "COMPLETED", "receipt_id": receipt_id}
            audit(session, user_id, "DEMO_INCOMING_POSTED", result)
            return result

    def payroll(self, user_id, event_id, amount_fen):
        with self.db.write() as session:
            self.require_demo_admin(session, user_id)
            key = "payroll:" + user_id + ":" + event_id
            digest = hashlib.sha256(canonical({"amount_fen": amount_fen}).encode()).hexdigest()
            receipt = session.scalar(select(Receipt).where(Receipt.idempotency_key == key))
            if receipt:
                if receipt.params_hash != digest:
                    raise BankError("IDEMPOTENCY_CONFLICT", "同一工资事件编号的金额不可改变", 409)
                return receipt.result
            receipt_id = uid("receipt")
            transaction_id = self.bank._transaction(
                session,
                user_id,
                receipt_id,
                amount_fen,
                "sys_payroll",
                primary_account(session, user_id).id,
                "PAYROLL",
                "模拟工资到账",
                "income",
            )
            result = {
                "receipt_id": receipt_id,
                "transaction_id": transaction_id,
                "amount_fen": amount_fen,
                "balance_fen": primary_account(session, user_id).balance_fen,
                "execution_mode": "sandbox",
            }
            session.add(
                Receipt(
                    id=receipt_id,
                    owner_id=user_id,
                    operation="event.payroll",
                    idempotency_key=key,
                    params_hash=digest,
                    result=result,
                    created_at=business_now(session).isoformat(),
                )
            )
            audit(session, user_id, "PAYROLL_EVENT", result)
            return result

    @staticmethod
    def _next_month(day):
        year, month = day.year + (day.month == 12), day.month % 12 + 1
        return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))

    def advance(self, user_id, days):
        with self.db.write() as session:
            self.require_demo_admin(session, user_id)
            clock = session.get(Document, "business_clock")
            current = business_now(session) + timedelta(days=days)
            clock.data = {"now": current.isoformat()}
            audit(session, user_id, "DEMO_CLOCK_ADVANCED", {"days": days, "business_now": current.isoformat()})
        return self.tick()

    def tick(self):
        with self.graph.process_locks.hold("scheduler:tick"):
            return self._tick_locked()

    def _tick_locked(self):
        notices = []
        with self.db.write() as session:
            current = business_now(session)
            for redemption in documents(session, "redemption"):
                if (
                    redemption.data["status"] == "PENDING"
                    and redemption.data["settles_on"] <= current.date().isoformat()
                ):
                    self.bank.settle_redemption(session, redemption.owner_id, redemption)
                    audit(session, redemption.owner_id, "REDEMPTION_SETTLED", redemption.data)
            for contract in documents(session, "subscription"):
                value = dict(contract.data)
                due = date.fromisoformat(value["next_debit"])
                active = value["bank_mandate"] == value["merchant_contract"] == "ACTIVE"
                if active and 0 <= (due - current.date()).days <= value["reminder_days"]:
                    key = f"reminder:{contract.id}:{due}"
                    if session.get(Document, key) is None:
                        notice = {
                            "id": key,
                            "type": "subscription_renewal",
                            "name": value["name"],
                            "amount_fen": value["amount_fen"],
                            "due": due.isoformat(),
                            "status": "UNREAD",
                        }
                        save_document(session, key, "notification", contract.owner_id, notice)
                        audit(session, contract.owner_id, "RENEWAL_REMINDER", notice)
                        notices.append(notice)
                while due <= current.date():
                    key = f"debit:{contract.id}:{due}"
                    if session.get(Document, key) is None:
                        status, tx = "SKIPPED_CANCELLED", None
                        # Seeded contracts are pre-existing recurring mandates; the agent cannot create one here.
                        if active:
                            if (
                                not session.get(User, contract.owner_id).locked
                                and available(session, contract.owner_id) >= value["amount_fen"]
                            ):
                                event = self.bank.money_event_once(
                                    session, contract.owner_id, key, "event.subscription",
                                    {"subscription_id": contract.id, "due": due.isoformat(), "amount_fen": value["amount_fen"]},
                                    primary_account(session, contract.owner_id).id,
                                    "sys_merchants",
                                    "PAYMENT",
                                    value["name"],
                                    "subscription",
                                    value["merchant_id"],
                                )
                                tx = event["data"]["transaction_id"]
                                status = "CHARGED"
                            else:
                                status = "DECLINED"
                        outcome = {
                            "subscription_id": contract.id,
                            "status": status,
                            "transaction_id": tx,
                            "amount_fen": value["amount_fen"],
                            "due": due.isoformat(),
                            "execution_mode": "sandbox",
                            "receipt_id": key if status == "CHARGED" else None,
                        }
                        save_document(session, key, "debit_event", contract.owner_id, outcome)
                        audit(session, contract.owner_id, "SUBSCRIPTION_DEBIT", outcome)
                    due = self._next_month(due)
                contract.data = {**value, "next_debit": due.isoformat()}
            candidates = session.execute(
                select(Plan.user_id, Plan.id, Node.scheduled_for)
                .join(Node, Node.plan_id == Plan.id)
                .where(Node.status == "WAIT_TIME", Plan.status.not_in(["COMPLETED", "CANCELLED", "PARTIAL"]))
            ).all()
            pending = list(
                dict.fromkeys(
                    (owner, key) for owner, key, when in candidates if when and datetime.fromisoformat(when) <= current
                )
            )
        outcomes = []
        for owner, plan_id in pending:
            result = self.graph.resume(owner, plan_id)
            outcomes.append({"plan_id": plan_id, "status": result["status"]})
        return {
            "business_now": current.isoformat(),
            "notifications": notices,
            "plans": outcomes,
            "execution_mode": "sandbox",
        }
