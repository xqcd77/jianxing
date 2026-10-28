from collections import defaultdict
from sqlalchemy import select
from ..bank_rules import owned_document
from ..errors import BankError, ClarificationNeeded
from ..storage import Transaction, documents, save_document, uid
from ..resources import contract_version


class SubscriptionsService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt_id):
        if operation == "subscriptions.detect":
            merchants = defaultdict(list)
            for row in session.scalars(
                select(Transaction).where(Transaction.owner_id == user_id, Transaction.kind == "PAYMENT")
            ):
                if row.merchant_id:
                    merchants[row.merchant_id].append(row)
            recurring = [
                {
                    "merchant_id": merchant,
                    "amount_fen": rows[-1].amount_fen,
                    "description": rows[-1].subject,
                    "observed_months": len({row.posted_at[:7] for row in rows}),
                    "status": "CANDIDATE_REQUIRES_CONTRACT_CHECK",
                    "transaction_ids": [row.id for row in rows],
                    "category": rows[-1].category,
                }
                for merchant, rows in merchants.items()
                if len({row.posted_at[:7] for row in rows}) >= 3
            ]
            return {
                "candidates": [r for r in recurring if r["category"] == "subscription"],
                "other_recurring_bills": [r for r in recurring if r["category"] in ["housing", "utilities"]],
                "method": "historical_category_and_monthly_recurrence_not_contract_proof",
            }
        if operation == "subscriptions.query":
            rows = documents(session, "subscription", user_id)
            hint = params.get("name_hint", "").strip().casefold()
            if not hint:
                return {"subscriptions": [doc.data for doc in rows]}
            matched = [doc for doc in rows if hint in {str(doc.id).casefold(), doc.data["name"].casefold(),
                                                       doc.data["merchant_id"].casefold()}]
            if len(matched) != 1:
                raise ClarificationNeeded("没有找到唯一的本人订阅合同，请核对订阅名称，或先查询合同列表再选择。")
            row = matched[0]
            return {"subscriptions": [row.data], "selected": {"subscription_id": row.id,
                "name": row.data["name"], "contract_version": contract_version(row.data)}}
        contract = owned_document(session, params["subscription_id"], "subscription", user_id)
        if operation == "subscriptions.cancel" and params.get("assistance_case_id"):
            from ..support import validate_assistance
            validate_assistance(session, user_id, params, contract)
        value = dict(contract.data)
        if params.get("contract_version") and params["contract_version"] != contract_version(value):
            raise BankError("CONTRACT_CHANGED", "合同条件已变化，请重新查询并核对后办理", 409)
        if operation == "subscriptions.remind":
            value["reminder_days"] = params["days_before"]
            contract.data = value
            return {"subscription_id": contract.id, "days_before": value["reminder_days"], "channel": "in_app",
                    "name": value["name"], "amount_fen": value["amount_fen"], "next_debit": value["next_debit"],
                    "bank_mandate": value["bank_mandate"], "merchant_contract": value["merchant_contract"]}
        if params["scope"] not in ["bank", "merchant", "both"]:
            raise BankError("INVALID_CANCEL_SCOPE", "取消范围必须为bank、merchant或both")
        if params["scope"] in ["bank", "both"]:
            value["bank_mandate"] = "REVOKED"
        if params["scope"] in ["merchant", "both"]:
            if value["merchant_contract"] != "CANCELLED" and not value["cancellation_supported"] and not params.get("assistance_case_id"):
                if params["scope"] == "merchant":
                    raise BankError("MERCHANT_UNSUPPORTED", "商户不支持自动取消，请人工办理合同；银行代扣未修改")
                contract.data = value
                notice = {
                    "id": uid("notice"),
                    "type": "merchant_cancellation_required",
                    "name": value["name"],
                    "message": "银行代扣已撤销；商户合同仍有效，请人工确认取消与可能的合同义务。",
                    "status": "UNREAD",
                }
                save_document(session, notice["id"], "notification", user_id, notice)
                return {
                    **value,
                    "refund_fen": 0,
                    "requires_human": True,
                    "outcome": "PARTIAL",
                    "message": notice["message"],
                    "current_balance_unchanged": True,
                }
            value["merchant_contract"] = "CANCELLED"
        contract.data = value
        return {**value, "refund_fen": 0, "current_balance_unchanged": True}
