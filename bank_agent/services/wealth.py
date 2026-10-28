from datetime import timedelta
from ..bank_rules import available, business_now, primary_account
from ..errors import BankError, ClarificationNeeded
from ..storage import Document, User, documents, save_document, uid
from ..banking_calendar import settlement_date
from ..risk import product_disclosure


def holding_lots(value):
    if "lots" in value:
        return [dict(lot) for lot in value["lots"]]
    if not value.get("units_milli"):
        return []
    return [{"id": "legacy", "units_milli": value["units_milli"], "principal_fen": value["principal_fen"],
             "available_after": value["available_after"], "source": "legacy_aggregate"}]


def aggregate_holding(product_id, lots, fallback_date):
    positive = [lot for lot in lots if lot["units_milli"] > 0]
    return {"product_id": product_id, "lots": lots,
        "units_milli": sum(lot["units_milli"] for lot in lots),
        "principal_fen": sum(lot["principal_fen"] for lot in lots),
        "available_after": min((lot["available_after"] for lot in positive), default=fallback_date),
        "lot_policy": "fifo_among_unlocked_lots"}


class WealthService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt):
        user = session.get(User, user_id)
        clock = business_now(session)
        if operation == "wealth.compare":
            products = [doc.data for doc in documents(session, "product")]
            assessment_valid = user.risk_valid_until >= clock.date().isoformat()
            funds = available(session, user_id)
            comparisons = []
            for product in products:
                reasons = []
                if not assessment_valid:
                    reasons.append("风险测评已过期，请先重新测评")
                if product["risk_level"] > user.risk_level:
                    reasons.append("产品风险超过当前测评等级")
                if product["minimum_fen"] > funds:
                    reasons.append("当前可用资金未达到起购金额")
                comparisons.append({**product, "eligible": not reasons, "eligibility_reasons": reasons,
                                    "risk_matched": product["risk_level"] <= user.risk_level})
            candidates = [
                p
                for p in products
                if p["risk_level"] <= user.risk_level
                and user.risk_valid_until >= clock.date().isoformat()
                and p["minimum_fen"] <= available(session, user_id)
            ]
            candidates.sort(key=lambda p: (p["lock_days"], p["redeem_business_days"], p["risk_level"]))
            return {
                "risk_level": user.risk_level,
                "risk_valid_until": user.risk_valid_until,
                "products": comparisons,
                "source": "simulated_product_catalog",
                "recommended_product_ids": [p["id"] for p in candidates],
                "recommendation_method": "risk_and_minimum_filter_then_liquidity_order",
                "recommendation_reason": "先满足测评有效期、风险与可用资金约束，再优先流动性；不承诺收益",
            }
        if operation == "wealth.assess":
            if len(params["answers"]) != 5:
                raise BankError("INVALID_ASSESSMENT", "模拟问卷必须回答5道题，每题1至5分")
            level = max(1, min(5, sum(params["answers"]) // 5))
            user.risk_level = level
            user.risk_valid_until = (clock.date() + timedelta(days=365)).isoformat()
            return {
                "risk_level": level,
                "valid_until": user.risk_valid_until,
                "method": "sandbox_questionnaire_not_production_suitability",
            }
        holdings = documents(session, "holding", user_id)
        if operation == "wealth.holdings":
            positions = []
            for doc in holdings:
                product = session.get(Document, doc.data["product_id"]).data
                lots = holding_lots(doc.data)
                unlocked = sum(lot["units_milli"] for lot in lots if lot["available_after"] <= clock.date().isoformat())
                market = doc.data["units_milli"] * product["nav_1e4"] // 100000
                positions.append(
                    {
                        **doc.data,
                        "market_value_fen": market,
                        "unrealized_gain_fen": market - doc.data["principal_fen"],
                        "nav_1e4": product["nav_1e4"],
                        "source": "sandbox_product_nav",
                        "lots": lots, "redeemable_units_milli": unlocked,
                        "redeemable_value_fen": unlocked * product["nav_1e4"] // 100000,
                        "next_unlock_date": min((lot["available_after"] for lot in lots
                            if lot["units_milli"] > 0 and lot["available_after"] > clock.date().isoformat()), default=None),
                    }
                )
            return {
                "holdings": positions,
                "redemptions": [doc.data for doc in documents(session, "redemption", user_id)],
            }
        product_doc = session.get(Document, params["product_id"])
        if not product_doc or product_doc.kind != "product":
            raise ClarificationNeeded("请选择一个有效产品：P_CASH、P_STEADY或P_LOCKED")
        product = product_doc.data
        if (type(product.get("nav_1e4")) is not int or product["nav_1e4"] <= 0
                or not 0 <= product.get("fee_bps", 0) < 10000):
            raise BankError("PRODUCT_NAV_INVALID", "产品净值或费率数据无效，请核查产品资料后办理")
        amount = params["amount_fen"]
        holding_id = f"holding_{user_id}_{product['id']}"
        holding = session.get(Document, holding_id)
        if operation == "wealth.purchase":
            product_disclosure(session, product["id"])
            if user.risk_valid_until < clock.date().isoformat():
                raise BankError("ASSESSMENT_EXPIRED", "风险测评已过期，请重新测评")
            if product["risk_level"] > user.risk_level:
                raise BankError("RISK_MISMATCH", f"所选产品风险等级R{product['risk_level']}超过当前测评等级R{user.risk_level}")
            if amount < product["minimum_fen"] or amount > available(session, user_id):
                raise BankError("PURCHASE_CONSTRAINT", "未达到起购金额或可用资金不足")
            tx = self.bank._transaction(
                session,
                user_id,
                receipt,
                amount,
                primary_account(session, user_id).id,
                "sys_wealth",
                "WEALTH_PURCHASE",
                f"申购{product['name']}",
                "investment",
            )
            units = amount * 100000 // product["nav_1e4"]
            lots = holding_lots(holding.data) if holding else []
            lots.append({"id": receipt, "units_milli": units, "principal_fen": amount,
                "purchased_at": clock.isoformat(), "available_after": (clock.date() + timedelta(days=product["lock_days"])).isoformat(),
                "source": "bank_purchase_receipt"})
            save_document(
                session,
                holding_id,
                "holding",
                user_id,
                aggregate_holding(product["id"], lots, clock.date().isoformat()),
            )
            return {
                "transaction_id": tx,
                "product_id": product["id"],
                "amount_fen": amount,
                "units_milli": units,
                "balance_fen": primary_account(session, user_id).balance_fen,
            }
        units = (amount * 100000 + product["nav_1e4"] - 1) // product["nav_1e4"]
        if not holding or holding.data["units_milli"] < units or units <= 0:
            raise BankError("INSUFFICIENT_HOLDING", "没有足够的可赎回持仓")
        lots = holding_lots(holding.data)
        unlocked = [lot for lot in lots if lot["available_after"] <= clock.date().isoformat()]
        if sum(lot["units_milli"] for lot in unlocked) < units:
            next_day = min(lot["available_after"] for lot in lots if lot["units_milli"] > 0 and lot not in unlocked)
            raise BankError("PRODUCT_LOCKED", f"可赎回份额不足，其他份额锁定至{next_day}；持仓仍然保留")
        remaining, cost_basis, allocations = units, 0, []
        for lot in unlocked:
            taken = min(remaining, lot["units_milli"])
            cost = lot["principal_fen"] * taken // lot["units_milli"] if taken else 0
            lot["units_milli"] -= taken
            lot["principal_fen"] -= cost
            remaining -= taken
            cost_basis += cost
            if taken:
                allocations.append({"lot_id": lot["id"], "units_milli": taken, "cost_basis_fen": cost})
            if not remaining:
                break
        holding.data = aggregate_holding(product["id"], lots, clock.date().isoformat())
        settlement = settlement_date(session, clock, product["redeem_business_days"], product.get("cutoff_hour", 15))
        redemption_id = uid("redeem")
        fee = amount * product["fee_bps"] // 10000
        value = {
            "id": redemption_id,
            "product_id": product["id"],
            "amount_fen": amount - fee,
            "fee_fen": fee,
            "redeemed_cost_basis_fen": cost_basis,
            "realized_gain_fen": amount - fee - cost_basis,
            "settles_on": settlement.isoformat(),
            "status": "PENDING",
            "receipt_id": receipt,
            "lot_allocations": allocations,
            "calendar_source": "configured_sandbox_calendar_not_official_holiday_feed",
        }
        save_document(session, redemption_id, "redemption", user_id, value)
        if settlement <= clock.date():
            self.settle_redemption(session, user_id, session.get(Document, redemption_id))
            value = session.get(Document, redemption_id).data
        return {**value, "cash_available_only_after_settlement": True}

    def settle_redemption(self, session, user_id, redemption):
        if redemption.data["status"] != "PENDING":
            return
        value = redemption.data
        event = self.bank.money_event_once(session, user_id, f"settlement:{value['id']}", "event.redemption",
            {"redemption_id": value["id"], "amount_fen": value["amount_fen"]},
            "sys_wealth", primary_account(session, user_id).id, "WEALTH_REDEMPTION", "赎回到账", "investment")
        redemption.data = {**value, "status": "SETTLED", "settled_at": business_now(session).isoformat(),
                           "settlement_receipt_id": event["receipt_id"]}
