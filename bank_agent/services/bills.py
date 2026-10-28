from collections import defaultdict
from datetime import date, timedelta
from sqlalchemy import select
from ..bank_rules import available, business_now
from ..errors import BankError, ClarificationNeeded
from ..storage import Transaction, documents, save_document, uid


class BillsService:
    def __init__(self, bank):
        self.bank = bank

    def execute(self, session, user_id, operation, params, receipt_id):
        clock = business_now(session)
        if operation == "bills.report":
            rows = list(
                session.scalars(
                    select(Transaction).where(
                        Transaction.owner_id == user_id,
                        Transaction.posted_at.startswith(
                            params["period"][:4] if params["annual"] else params["period"]
                        ),
                    )
                )
            )
            categories, monthly = defaultdict(int), defaultdict(int)
            income = incoming = refunds = transfers = gross = 0
            for row in rows:
                if row.kind == "PAYROLL":
                    income += row.amount_fen
                elif row.kind == "INCOMING":
                    incoming += row.amount_fen
                elif row.kind == "REFUND":
                    refunds += row.amount_fen
                    categories[row.category] -= row.amount_fen
                    monthly[row.posted_at[:7]] -= row.amount_fen
                elif row.kind in ["TRANSFER", "AA_PAYMENT"]:
                    transfers += row.amount_fen
                elif row.kind == "PAYMENT":
                    gross += row.amount_fen
                    categories[row.category] += row.amount_fen
                    monthly[row.posted_at[:7]] += row.amount_fen
            return {
                "period": params["period"],
                "annual": params["annual"],
                "income_fen": income,
                "incoming_fen": incoming,
                "total_inflow_fen": income + incoming,
                "consumption_fen": sum(categories.values()),
                "gross_consumption_fen": gross,
                "refund_fen": refunds,
                "transfer_fen": transfers,
                "categories": dict(categories),
                "monthly": dict(monthly),
                "transaction_ids": [row.id for row in rows],
                "covered_months": sorted({row.posted_at[:7] for row in rows}),
                "as_of": clock.isoformat(),
                "source": "sandbox_transaction_ledger",
                "category_method": "ledger_labels_with_refund_netting",
            }
        if operation == "bills.anomalies":
            groups = defaultdict(list)
            for row in session.scalars(
                select(Transaction).where(Transaction.owner_id == user_id, Transaction.kind == "PAYMENT")
            ):
                groups[(row.merchant_id, row.amount_fen, row.posted_at[:10])].append(row)
            return {
                "alerts": [
                    {"reason": "同一商户同日相同金额重复扣款，需用户核实", "transaction_ids": [row.id for row in rows],
                     "transactions": [{"transaction_id": row.id, "date": row.posted_at[:10],
                         "description": row.subject, "amount_fen": row.amount_fen} for row in rows]}
                    for rows in groups.values()
                    if len(rows) > 1
                ],
                "classification": "suspected_not_confirmed_fraud",
            }
        if operation == "cashflow.income_plan":
            if params["date"] <= clock.date().isoformat():
                raise ClarificationNeeded("预计收入日期必须是未来；实际工资入账以银行事件为准")
            key = uid("income_plan")
            value = {"id": key, **params, "source": "user_confirmed", "counted_as_spendable": False}
            save_document(session, key, "expected_income", user_id, value)
            return value
        if operation == "cashflow.project":
            current = available(session, user_id)
            horizon = clock.date() + timedelta(days=params["days"])
            obligations = []
            for doc in documents(session, "subscription", user_id):
                value = doc.data
                if value["bank_mandate"] != "ACTIVE" or value["merchant_contract"] != "ACTIVE":
                    continue
                due = date.fromisoformat(value["next_debit"])
                while due <= horizon:
                    if due >= clock.date():
                        obligations.append(
                            {
                                "date": due.isoformat(),
                                "amount_fen": value["amount_fen"],
                                "source": doc.id,
                                "name": value["name"],
                                "kind": "subscription",
                            }
                        )
                    import calendar

                    year, month = due.year + int(due.month == 12), due.month % 12 + 1
                    due = date(year, month, min(due.day, calendar.monthrange(year, month)[1]))
            expected = sum(item["amount_fen"] for item in obligations)
            stresses = [
                {**item, "amount_fen": item["amount_fen"] * (100 + params["expense_increase_pct"]) // 100}
                for item in obligations
            ]
            stressed = sum(item["amount_fen"] for item in stresses)
            # Indicative future income can change the scenario, never spendable bank cash.
            forecasts = []
            for doc in documents(session, "expected_income", user_id):
                if doc.data.get("source") != "user_confirmed":
                    continue
                due = date.fromisoformat(doc.data["date"]) + timedelta(days=params["income_delay_days"])
                if clock.date() <= due <= horizon:
                    forecasts.append({"date": due.isoformat(), "amount_fen": doc.data["amount_fen"], "source": doc.id})
            timeline, projection, minimum = [], current, current
            dates = sorted({item["date"] for item in [*stresses, *forecasts]})
            for day in dates:
                # Conservative ordering: charge all same-day obligations before indicative income.
                projection -= sum(item["amount_fen"] for item in stresses if item["date"] == day)
                minimum = min(minimum, projection)
                projection += sum(item["amount_fen"] for item in forecasts if item["date"] == day)
                timeline.append({"date": day, "projected_fen": projection, "minimum_so_far_fen": minimum})
            return {
                "available_fen": current,
                "known_future_debits_fen": expected,
                "stressed_available_fen": current - stressed,
                "minimum_projected_fen": minimum,
                "timeline": timeline,
                "obligations": obligations,
                "indicative_income": forecasts,
                "shortfall_fen": max(0, -minimum),
                "income_delay_days": params["income_delay_days"],
                "horizon_days": params["days"],
                "method": "deterministic_dated_obligations_and_stress",
                "coverage": "registered_subscriptions_and_user_confirmed_income_only",
                "unposted_income_counted_as_available": False,
            }
        raise BankError("TOOL_NOT_ALLOWED", "账单业务不存在")
