from datetime import date, timedelta
from ..bank_rules import business_now, owned_document, primary_account, card_daily_spent
from ..errors import BankError, ClarificationNeeded
from ..storage import Document, documents, save_document, uid


class LifeService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt):
        clock = business_now(session)
        if operation == "life.catalog":
            return {"items": [doc.data for doc in documents(session, "catalog")]}
        if operation == "life.orders":
            return {
                "orders": [doc.data for doc in documents(session, "order", user_id)],
                "goals": [doc.data for doc in documents(session, "goal", user_id)],
            }
        if operation == "life.goal":
            birthday = date.fromisoformat(params["birthday_date"])
            order_day = birthday - timedelta(days=2)
            if order_day < birthday.replace(day=1):
                raise ClarificationNeeded("月初生日与当月冻结/提前两天下单存在冲突，请明确允许提前冻结或修改日期")
            if order_day < clock.date():
                raise ClarificationNeeded("下单日期已过去，请修改生日或采购日期")
            goal_id = uid("goal")
            value = {"id": goal_id, **params, "order_date": order_day.isoformat(), "status": "PLANNED", "hold_id": ""}
            save_document(session, goal_id, "goal", user_id, value)
            return {"goal_id": goal_id, **value}
        if operation == "life.cancel_order":
            order = owned_document(session, params["order_id"], "order", user_id)
            if order.data["status"] != "CONFIRMED":
                raise BankError("ORDER_NOT_CANCELLABLE", "订单状态不支持取消或已取消")
            value = dict(order.data)
            tx = self.bank._transaction(
                session,
                user_id,
                receipt,
                value["amount_fen"],
                "sys_merchants",
                primary_account(session, user_id).id,
                "REFUND",
                "模拟订单退款",
                "gifts",
                value["merchant_id"],
            )
            order.data = {**value, "status": "REFUNDED", "refund_transaction_id": tx}
            item = session.get(Document, value["item_id"])
            item.data = {**item.data, "stock": item.data["stock"] + 1}
            return {"order_id": order.id, "status": "REFUNDED", "transaction_id": tx, "amount_fen": value["amount_fen"]}
        goal = owned_document(session, params["goal_id"], "goal", user_id)
        if clock.date().isoformat() > goal.data["birthday_date"]:
            raise ClarificationNeeded("生日已过，不能为过去日期下单；请取消此计划并重新确认配送日期")
        if goal.data["status"] != "FUNDED" or not goal.data["hold_id"]:
            raise BankError("GOAL_NOT_FUNDED", "生日预算尚未锁定")
        if clock.date().isoformat() < goal.data["order_date"]:
            raise BankError("TOO_EARLY", "尚未到生日前两天")
        quote = params["quote"]
        if quote.get("expires_at", "") < clock.isoformat():
            raise BankError("QUOTE_EXPIRED", "报价已过期，需要重新报价与确认")
        items = quote.get("items", [])
        if len(items) != 2 or {item.get("id") for item in items} != {"FLOWERS", "CAKE"}:
            raise BankError("INVALID_QUOTE", "生日采购必须包含已确认的鲜花和蛋糕")
        total = 0
        for item in items:
            current = session.get(Document, item["id"])
            if current.data["stock"] <= 0:
                raise BankError("OUT_OF_STOCK", "商品缺货，需要重新选择或人工接管")
            if any(item[key] != current.data[key] for key in ["price_fen", "delivery_fen", "merchant_id"]):
                raise BankError("PRICE_CHANGED", "商户报价变化，旧授权失效，需要重新确认")
            total += current.data["price_fen"] + current.data["delivery_fen"]
        if total != quote.get("total_fen") or quote.get("address") != goal.data["address"]:
            raise BankError("QUOTE_TAMPERED", "报价金额或配送地址不匹配")
        hold = owned_document(session, goal.data["hold_id"], "hold", user_id)
        if total > hold.data["remaining_fen"] or total > goal.data["budget_fen"]:
            raise BankError("BUDGET_EXCEEDED", "总价（含配送费）超过生日预算")
        card = owned_document(session, params["card_id"], "card", user_id)
        if card.data["status"] != "ACTIVE" or "gifts" in card.data["blocked_categories"]:
            raise BankError("CARD_RESTRICTED", "付款卡不可用或礼品交易被限制")
        if (
            total > card.data["single_limit_fen"]
            or card_daily_spent(session, user_id, card.id) + total > card.data["daily_limit_fen"]
        ):
            raise BankError("CARD_LIMIT", "订单付款超过卡片交易限制")
        tx = self.bank._transaction(
            session,
            user_id,
            receipt,
            total,
            primary_account(session, user_id).id,
            "sys_merchants",
            "PAYMENT",
            "生日鲜花和蛋糕",
            "gifts",
            "birthday_bundle",
        )
        save_document(
            session,
            f"card_payment_{receipt}",
            "card_payment",
            user_id,
            {"card_id": card.id, "amount_fen": total, "posted_at": clock.isoformat(), "transaction_id": tx},
        )
        orders = []
        for item in items:
            order_id = uid("order")
            value = {
                "id": order_id,
                "goal_id": goal.id,
                "item_id": item["id"],
                "merchant_id": item["merchant_id"],
                "amount_fen": item["price_fen"] + item["delivery_fen"],
                "status": "CONFIRMED",
                "transaction_id": tx,
                "delivery_date": goal.data["birthday_date"],
                "address": goal.data["address"],
            }
            save_document(session, order_id, "order", user_id, value)
            current = session.get(Document, item["id"])
            current.data = {**current.data, "stock": current.data["stock"] - 1}
            orders.append(value)
        remainder = hold.data["remaining_fen"] - total
        hold.data = {**hold.data, "status": "RELEASED", "remaining_fen": 0}
        goal.data = {**goal.data, "status": "COMPLETED", "spent_fen": total, "released_fen": remainder}
        return {
            "transaction_id": tx,
            "spent_fen": total,
            "released_fen": remainder,
            "orders": orders,
            "balance_fen": primary_account(session, user_id).balance_fen,
        }
