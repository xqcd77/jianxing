from ..bank_rules import available, held_total, owned_document, primary_account, resolve_contact, daily_outgoing
from ..errors import BankError
from ..storage import Document, User, save_document, uid


class FundsService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt_id):
        account = primary_account(session, user_id)
        if operation == "balance.query":
            return {
                "balance_fen": account.balance_fen,
                "reserved_fen": held_total(session, user_id),
                "available_fen": available(session, user_id),
                "account_id": account.id,
            }
        if operation == "funds.reserve":
            if params["goal_id"]:
                goal = owned_document(session, params["goal_id"], "goal", user_id)
                if goal.data["status"] not in ["PLANNED", "UNFUNDED"]:
                    raise BankError("GOAL_ALREADY_FUNDED", "该目标已预留、完成或取消，不能重复冻结")
                if params["amount_fen"] != goal.data["budget_fen"]:
                    raise BankError("GOAL_BUDGET_MISMATCH", "预留金额必须与已确认的目标预算一致")
            if available(session, user_id) < params["amount_fen"]:
                raise BankError("INSUFFICIENT_AVAILABLE", "可用余额不足，不能占用其他预留预算")
            hold_id = uid("hold")
            save_document(
                session,
                hold_id,
                "hold",
                user_id,
                {
                    "id": hold_id,
                    "purpose": params["purpose"],
                    "remaining_fen": params["amount_fen"],
                    "amount_fen": params["amount_fen"],
                    "status": "ACTIVE",
                    "goal_id": params["goal_id"],
                },
            )
            if params["goal_id"]:
                goal = owned_document(session, params["goal_id"], "goal", user_id)
                goal.data = {**goal.data, "hold_id": hold_id, "status": "FUNDED"}
            return {
                "hold_id": hold_id,
                "reserved_fen": params["amount_fen"],
                "available_fen": available(session, user_id),
            }
        if operation == "funds.release":
            hold = owned_document(session, params["hold_id"], "hold", user_id)
            released = hold.data["remaining_fen"] if hold.data["status"] == "ACTIVE" else 0
            hold.data = {**hold.data, "status": "RELEASED", "remaining_fen": 0}
            if hold.data["goal_id"]:
                goal = owned_document(session, hold.data["goal_id"], "goal", user_id)
                if goal.data["status"] == "FUNDED":
                    goal.data = {**goal.data, "status": "UNFUNDED"}
            return {"released_fen": released, "available_fen": available(session, user_id)}
        if operation == "funds.transfer":
            contact = resolve_contact(session, user_id, params["recipient_hint"])
            if available(session, user_id) < params["amount_fen"]:
                raise BankError("INSUFFICIENT_AVAILABLE", "可用余额不足或资金已预留")
            tx = self.bank._transaction(
                session,
                user_id,
                receipt_id,
                params["amount_fen"],
                account.id,
                contact["account_id"],
                "TRANSFER",
                params["subject"],
                "transfer",
            )
            return {
                "transaction_id": tx,
                "recipient": contact["name"],
                "amount_fen": params["amount_fen"],
                "balance_fen": account.balance_fen,
                "daily_outgoing_fen": daily_outgoing(session, user_id),
            }
        if operation.startswith("aa."):
            return self._aa(session, user_id, operation, params, receipt_id)
        raise BankError("TOOL_NOT_ALLOWED", "资金业务不存在")

    def _aa(self, session, user_id, operation, params, receipt):
        if operation == "aa.create":
            participants = list(dict.fromkeys(params["participants"]))
            if user_id not in participants or len(participants) < 2:
                raise BankError("INVALID_PARTICIPANTS", "AA至少两人，须包含发起人")
            if any(session.get(User, member) is None for member in participants):
                raise BankError("UNKNOWN_PARTICIPANT", "AA参与者必须是已登记的沙箱用户")
            base, remainder = divmod(params["amount_fen"], len(participants))
            request_id = uid("aa")
            shares = [
                {
                    "user_id": member,
                    "amount_fen": base + int(index < remainder),
                    "status": "SELF_SHARE" if member == user_id else "UNPAID",
                }
                for index, member in enumerate(participants)
            ]
            save_document(
                session,
                request_id,
                "aa",
                user_id,
                {
                    "id": request_id,
                    "total_fen": params["amount_fen"],
                    "subject": params["subject"],
                    "shares": shares,
                    "owner": user_id,
                    "creator_share_collectible": False,
                    "bill_source": "user_declared_amount_not_bank_verified",
                },
            )
            return {"request_id": request_id, "total_fen": params["amount_fen"], "shares": shares}
        request = session.get(Document, params["request_id"])
        if not request or request.kind != "aa" or user_id not in [row["user_id"] for row in request.data["shares"]]:
            raise BankError("RESOURCE_NOT_FOUND", "收款请求不存在或无权访问", 404)
        if operation == "aa.status":
            return request.data
        share = next(row for row in request.data["shares"] if row["user_id"] == user_id)
        if share["status"] in ["PAID", "SELF_SHARE"]:
            raise BankError("ALREADY_PAID", "本人的AA份额已经支付")
        if available(session, user_id) < share["amount_fen"]:
            raise BankError("INSUFFICIENT_AVAILABLE", "可用余额不足")
        tx = self.bank._transaction(
            session,
            user_id,
            receipt,
            share["amount_fen"],
            primary_account(session, user_id).id,
            primary_account(session, request.owner_id).id,
            "AA_PAYMENT",
            request.data["subject"],
            "transfer",
        )
        shares = [{**row, "status": "PAID"} if row["user_id"] == user_id else row for row in request.data["shares"]]
        request.data = {**request.data, "shares": shares}
        return {
            "transaction_id": tx,
            "amount_fen": share["amount_fen"],
            "settled": all(row["status"] in ["PAID", "SELF_SHARE"] for row in shares),
        }
