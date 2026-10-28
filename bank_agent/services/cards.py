from ..bank_rules import owned_document
from ..errors import BankError
from ..storage import documents, save_document, uid


class CardsService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt_id):
        if operation == "cards.query":
            return {"cards": [doc.data for doc in documents(session, "card", user_id)]}
        if operation == "cards.apply":
            if params["kind"] not in ["virtual", "physical"]:
                raise BankError("INVALID_CARD_KIND", "卡类型必须为virtual或physical")
            card_id = uid("card")
            value = {
                "id": card_id,
                "name": params["name"],
                "kind": params["kind"],
                "last4": f"{int(card_id[-4:], 16) % 10000:04d}",
                "funding_source": "debit_account",
                "limit_type": "bank_approved_payment_limit",
                "status": "ACTIVE" if params["kind"] == "virtual" else "APPROVED_PENDING_DELIVERY",
                "credit_limit_fen": 1000000,
                "single_limit_fen": 1000000,
                "daily_limit_fen": 1000000,
                "blocked_categories": [],
                "approval_source": "sandbox_bank_rules",
            }
            save_document(session, card_id, "card", user_id, value)
            return {"card_id": card_id, **value}
        card = owned_document(session, params["card_id"], "card", user_id)
        value = dict(card.data)
        if operation == "cards.limit":
            if params["amount_fen"] > 1200000:
                raise BankError("LIMIT_DECLINED", "模拟银行审批上限为12000元，Agent不能自行批准")
            value["credit_limit_fen"] = params["amount_fen"]
            value["single_limit_fen"] = min(value["single_limit_fen"], params["amount_fen"])
            value["daily_limit_fen"] = min(value["daily_limit_fen"], params["amount_fen"])
        elif operation == "cards.restrict":
            if max(params["single_limit_fen"], params["daily_limit_fen"]) > value["credit_limit_fen"]:
                raise BankError(
                    "CARD_LIMIT_EXCEEDS_APPROVAL", "交易限制不能超过银行已批准的交易额度，请先强验证申请调整额度"
                )
            value.update({key: params[key] for key in ["single_limit_fen", "daily_limit_fen", "blocked_categories"]})
        elif operation == "cards.unlock":
            if value["status"] != "ACTIVE":
                raise BankError("CARD_STATE_CONFLICT", "挂失卡须先强验证解挂，交易解锁不能替代解挂")
            value.update(
                single_limit_fen=value["credit_limit_fen"],
                daily_limit_fen=value["credit_limit_fen"],
                blocked_categories=[],
            )
        elif operation == "cards.lost":
            value["status"] = "LOST"
        elif operation == "cards.restore":
            if value["status"] != "LOST":
                raise BankError("CARD_STATE_CONFLICT", "此卡并未挂失")
            value["status"] = "ACTIVE"
        card.data = value
        return value
