from datetime import date, datetime, timedelta
import hashlib

from sqlalchemy import func, select

from .errors import BankError
from .seed import post_money
from .storage import (
    Document,
    LedgerEntry,
    Receipt,
    Transaction,
    User,
    canonical,
    documents,
    save_document,
    uid,
)

from .bank_rules import (
    DEFINITIONS as DEFINITIONS,
    PARAM_MODELS as PARAM_MODELS,
    Money as Money,
    Positive as Positive,
    business_now as business_now,
    primary_account as primary_account,
    held_total as held_total,
    available as available,
    owned_document as owned_document,
    resolve_contact as resolve_contact,
    daily_outgoing as daily_outgoing,
    risk_tier as risk_tier,
    card_daily_spent as card_daily_spent,
)
from .services.funds import FundsService
from .services.bills import BillsService
from .services.wealth import WealthService
from .services.cards import CardsService
from .services.subscriptions import SubscriptionsService
from .services.life import LifeService


class BankService:
    def __init__(self, db):
        self.db = db
        self.services = {"funds": FundsService(self), "bills": BillsService(self), "wealth": WealthService(self), "cards": CardsService(self), "subscriptions": SubscriptionsService(self), "life": LifeService(self)}

    def facts(self, user_id):
        with self.db.read() as session:
            account = primary_account(session, user_id)
            return {
                "execution_mode": "sandbox",
                "business_now": business_now(session).isoformat(),
                "account": {
                    "id": account.id,
                    "balance_fen": account.balance_fen,
                    "reserved_fen": held_total(session, user_id),
                    "available_fen": available(session, user_id),
                },
                "contacts": [doc.data for doc in documents(session, "contact", user_id)],
                "products": [doc.data for doc in documents(session, "product")],
                "cards": [doc.data for doc in documents(session, "card", user_id)],
                "life_events": [doc.data for doc in documents(session, "life_event", user_id)],
                # Imported, owner-scoped bank/merchant records retain provenance.
                # These are evidence only and never grant an operation permission.
                "financial_evidence_index": [{k: v for k, v in doc.data.items() if k not in {"body", "related_source_ids", "status", "visibility"}}
                    for doc in documents(session, "financial_evidence", user_id)],
                "risk_level": session.get(User, user_id).risk_level,
            }

    def validate_params(self, operation, params):
        from .memory import SENSITIVE

        if SENSITIVE.search(canonical(params)):
            raise BankError("SENSITIVE_BUSINESS_INPUT", "请在安全表单填写凭证，业务备注不能包含密码、验证码或密钥")
        if operation not in PARAM_MODELS:
            raise BankError("TOOL_NOT_ALLOWED", "这项业务目前不支持办理")
        try:
            if operation == "cards.restrict" and isinstance(params.get("blocked_categories"), list):
                aliases = {"礼品": "gifts", "礼物": "gifts", "gift": "gifts", "游戏": "gaming", "游戏充值": "gaming"}
                params = {
                    **params,
                    "blocked_categories": [aliases.get(value, value) for value in params["blocked_categories"]],
                }
            result = PARAM_MODELS[operation].model_validate(params).model_dump()
            if operation == "life.goal":
                date.fromisoformat(result["birthday_date"])
            if operation == "cashflow.income_plan":
                date.fromisoformat(result["date"])
            if operation == "bills.report":
                if result["annual"]:
                    date.fromisoformat(result["period"][:4] + "-01-01")
                else:
                    date.fromisoformat(result["period"] + "-01")
            return result
        except Exception as error:
            raise BankError("INVALID_TOOL_ARGUMENTS", "办理信息不完整或格式有误，请核对金额、日期和业务对象") from error

    def prepare(self, session, user_id, operation, params):
        params = self.validate_params(operation, params)
        if operation == "subscriptions.cancel" and params.get("assistance_case_id"):
            from .support import validate_assistance
            validate_assistance(session, user_id, params, owned_document(session, params["subscription_id"], "subscription", user_id))
        if operation in {"subscriptions.cancel", "subscriptions.remind"} and params.get("contract_version"):
            from .resources import contract_version
            contract = owned_document(session, params["subscription_id"], "subscription", user_id)
            if params["contract_version"] != contract_version(contract.data):
                raise BankError("CONTRACT_CHANGED", "合同条件已变化，请重新查询并核对后办理", 409)
        if operation == "funds.transfer":
            resolve_contact(session, user_id, params["recipient_hint"])
            funds = available(session, user_id)
            if funds < params["amount_fen"]:
                from .presentation import yuan

                raise BankError("INSUFFICIENT_AVAILABLE", f"当前可用余额为{yuan(funds)}，不足以支付这笔转账")
        if operation == "life.purchase":
            goal = owned_document(session, params["goal_id"], "goal", user_id)
            items = [session.get(Document, key).data for key in ["FLOWERS", "CAKE"]]
            current = params["quote"]
            if current:
                issued = session.get(Document, current.get("id", ""))
                if not issued or issued.kind != "quote" or issued.owner_id != user_id or issued.data != current:
                    raise BankError("UNTRUSTED_QUOTE", "报价必须来自当前用户的商户后端回执")
            prices = {item["id"]: (item["price_fen"], item["delivery_fen"], item["merchant_id"]) for item in items}
            expired = current and datetime.fromisoformat(current["expires_at"]) < business_now(session)
            changed = current and (
                current["address"] != goal.data["address"]
                or any(
                    prices.get(item["id"]) != (item["price_fen"], item["delivery_fen"], item["merchant_id"])
                    for item in current["items"]
                )
            )
            if not current or expired or changed:
                quote_id = uid("quote")
                current = {
                    "id": quote_id,
                    "items": [
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "merchant_id": item["merchant_id"],
                            "price_fen": item["price_fen"],
                            "delivery_fen": item["delivery_fen"],
                        }
                        for item in items
                    ],
                    "total_fen": sum(item["price_fen"] + item["delivery_fen"] for item in items),
                    "expires_at": (business_now(session) + timedelta(hours=1)).isoformat(),
                    "address": goal.data["address"],
                }
                save_document(session, quote_id, "quote", user_id, current)
            params["quote"] = current
        return params

    def _transaction(self, session, user_id, receipt, amount, sender, recipient, kind, subject, category, merchant=""):
        post_money(session, sender, recipient, amount, receipt)
        transaction = Transaction(
            id=uid("txn"),
            owner_id=user_id,
            kind=kind,
            amount_fen=amount,
            sender=sender,
            recipient=recipient,
            subject=subject,
            category=category,
            merchant_id=merchant,
            posted_at=business_now(session).isoformat(),
            receipt_id=receipt,
        )
        session.add(transaction)
        session.flush()
        return transaction.id

    def execute_once(self, session, user_id, operation, params, idempotency_key):
        digest = hashlib.sha256(canonical(params).encode()).hexdigest()
        existing = session.scalar(select(Receipt).where(Receipt.idempotency_key == idempotency_key))
        if existing:
            if existing.owner_id != user_id or existing.operation != operation or existing.params_hash != digest:
                raise BankError("IDEMPOTENCY_CONFLICT", "幂等键已经用于不同业务参数")
            return existing.result
        receipt_id = uid("receipt")
        value = self.dispatch(session, user_id, operation, params, receipt_id)
        result = {
            "receipt_id": receipt_id,
            "execution_mode": "sandbox",
            "operation": operation,
            "verified_by": "bank_backend",
            "business_time": business_now(session).isoformat(),
            "data": value,
        }
        session.add(
            Receipt(
                id=receipt_id,
                owner_id=user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                params_hash=digest,
                result=result,
                created_at=business_now(session).isoformat(),
            )
        )
        session.flush()
        # Enforce balanced posting for every money-moving receipt.
        balance = session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.delta_fen), 0)).where(LedgerEntry.receipt_id == receipt_id)
        )
        if balance != 0:
            raise BankError("LEDGER_INVARIANT", "账本资金守恒校验失败")
        return result

    def money_event_once(self, session, user_id, event_id, operation, params, sender, recipient, kind, subject, category, merchant=""):
        if operation not in {"event.subscription", "event.redemption"}:
            raise BankError("EVENT_NOT_ALLOWED", "自动资金事件不在允许范围")
        key, digest = "event:" + event_id, hashlib.sha256(canonical(params).encode()).hexdigest()
        previous = session.scalar(select(Receipt).where(Receipt.idempotency_key == key))
        if previous:
            if previous.owner_id != user_id or previous.operation != operation or previous.params_hash != digest:
                raise BankError("IDEMPOTENCY_CONFLICT", "原事件凭证与当前参数不一致")
            return previous.result
        tx = self._transaction(session, user_id, event_id, params["amount_fen"], sender, recipient, kind, subject, category, merchant)
        result = {"receipt_id": event_id, "execution_mode": "sandbox", "operation": operation,
            "verified_by": "bank_backend", "business_time": business_now(session).isoformat(),
            "data": {**params, "transaction_id": tx, "balance_fen": primary_account(session, user_id).balance_fen}}
        session.add(Receipt(id=event_id, owner_id=user_id, operation=operation, idempotency_key=key,
            params_hash=digest, result=result, created_at=business_now(session).isoformat()))
        session.flush()
        if session.scalar(select(func.coalesce(func.sum(LedgerEntry.delta_fen), 0)).where(LedgerEntry.receipt_id == event_id)):
            raise BankError("LEDGER_INVARIANT", "自动事件账本不守恒")
        return result

    def dispatch(self, session, user_id, operation, params, receipt_id):
        if operation not in DEFINITIONS:
            raise BankError("TOOL_NOT_ALLOWED", "业务不存在")
        return self.services[DEFINITIONS[operation][0]].execute(session, user_id, operation, params, receipt_id)

    def settle_redemption(self, session, user_id, redemption):
        return self.services["wealth"].settle_redemption(session, user_id, redemption)
