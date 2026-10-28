"""Read-only financial facts derived from actually retrieved evidence.

No task IDs, oracle access or transaction submission. Monetary quantities are
exported in yuan, with source references retained alongside each computation.
"""

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re


def yuan(fen):
    return format(Decimal(fen) / 100, "f")


def model_units(value):
    """Convert every explicit monetary/scaled field in the model's view once."""
    if isinstance(value, list):
        return [model_units(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if item is not None and key.endswith("_fen"):
            result[key[:-4] + "_yuan"] = yuan(item)
        elif item is not None and key.endswith("_bps"):
            result[key[:-4] + "_pct"] = format(Decimal(item) / 100, "f")
            result[key[:-4] + "_ratio"] = format(Decimal(item) / 10000, "f")
        elif item is not None and key == "nav_1e4":
            result["nav"] = format(Decimal(item) / 10000, "f")
        elif item is not None and key == "units_milli":
            result["shares"] = format(Decimal(item) / 1000, "f")
        else:
            result[key] = model_units(item)
    return result


def verified_financial_facts(state, *, deduplicate_expenses=False):
    evidence = list(state["evidence"].values())
    brief = state["brief"]
    query = state["query"]
    account = next(e for e in evidence if e["tool"] == "account")
    cf = next(e for e in evidence if e["tool"] == "cashflow.project")
    sources = {}
    for ev in evidence:
        for source in ev["data"].get("supporting_records", []):
            sources[source["source_id"]] = source
    facts = []
    available = account["data"]["available_fen"]
    new_reserve = brief["reserve_fen"] + brief["emergency_fen"]
    pending = sum(x["amount_fen"] for x in cf["data"]["pending_plans"])
    known = cf["data"]["known_future_debits_fen"]

    def already_registered(expense):
        matches = [
            x
            for x in cf["data"].get("obligations", [])
            if x["date"][:10] == expense["date"][:10] and x["amount_fen"] == expense["amount_fen"]
        ]
        # Same date and amount alone do not prove two business obligations are
        # the same. Require a unique compatible purpose as well.
        return len(matches) == 1 and (
            matches[0].get("name") == expense["label"]
            or expense["label"] in {"订阅", "已登记订阅", "代扣"}
            and matches[0].get("kind") == "subscription"
        )

    declared = sum(x["amount_fen"] for x in brief.get("expenses", []) if not already_registered(x))
    transfers = {}
    for m in re.finditer(r"(?:给|向)([^，,。；;\n\d]{1,16}?)(\d+(?:\.\d{1,2})?)\s*元?", query):
        name = re.sub(r"(?:转账|转|汇|改成|改为|安排|留)$", "", m[1]).strip()
        if name:
            transfers[name] = int(Decimal(m[2]) * 100)
    # An explicitly mentioned existing plan is already in the read-only pending
    # list. It is contingent, not a second new request or a posted expense.
    references_existing = bool(re.search(r'计划.{0,8}(?:还没执行|未执行|尚未执行)', query))
    new_transfer = sum(amount for name, amount in transfers.items() if not (
        references_existing and any(p['amount_fen'] == amount and p.get('recipient_hint') == name
                                    for p in cf['data']['pending_plans'])))
    if deduplicate_expenses:
        # A named transfer extracted into expenses is the same proposed outflow,
        # not an additional obligation. Match both recipient and amount.
        declared -= sum(
            x["amount_fen"]
            for x in brief.get("expenses", [])
            if not already_registered(x)
            and any(name in x["label"] and amount == x["amount_fen"] for name, amount in transfers.items())
        )
    ceiling = max(0, available - new_reserve - pending - known - new_transfer - declared)
    requested = brief.get("amount_fen") or 0
    facts.append(
        {
            "kind": "unified_budget",
            "evidence_ids": [account["id"], cf["id"]],
            "available_yuan": yuan(available),
            "existing_hold_already_excluded_yuan": yuan(account["data"]["balance_fen"] - available),
            "new_reserve_yuan": yuan(new_reserve),
            "existing_pending_plan_yuan": yuan(pending),
            'existing_pending_transfer_yuan': yuan(sum(p['amount_fen'] for p in cf['data']['pending_plans']
                if p.get('operation') == 'funds.transfer')),
            "new_proposed_transfer_yuan": yuan(new_transfer),
            "registered_future_dues_yuan": yuan(known),
            "additional_user_declared_expenses_yuan": yuan(declared),
            "maximum_new_investment_yuan": yuan(ceiling),
            "requested_investment_yuan": yuan(requested),
            "investment_shortfall_yuan": yuan(max(0, requested - ceiling)),
            "unallocated_after_all_targets_yuan": yuan(
                available - new_reserve - pending - known - new_transfer - declared - requested
            ),
            "whole_plan_required_yuan": yuan(new_reserve + pending + known + new_transfer + declared + requested),
            "whole_plan_gap_yuan": yuan(
                max(0, new_reserve + pending + known + new_transfer + declared + requested - available)
            ),
            "rule": "可用额已排除已有预留；已入账消费不可再扣；拟投目标不能静默改为上限。",
        }
    )
    for ev in evidence:
        if ev["tool"] != "product_terms":
            continue
        for item in ev["data"].get("products", []):
            p = item["catalog"]
            minimum = p.get("minimum_fen")
            if isinstance(minimum, int) and requested:
                facts.append(
                    {
                        "kind": "purchase_minimum_check",
                        "evidence_ids": [ev["id"]],
                        "product_name": p["name"],
                        "product_id": p["id"],
                        "requested_yuan": yuan(requested),
                        "minimum_yuan": yuan(minimum),
                        "meets_minimum": requested >= minimum,
                        "risk_level": p["risk_level"],
                        "matches_user_risk": p["risk_level"] <= account["data"]["risk_level"],
                        "rule": "新申购门槛不决定已有份额是否可以赎回；符合起购及风险不等于流动性满足。",
                    }
                )
        break
    # Full timestamp event records take precedence over the generic daily
    # projections. Baseline and stressed rows are calculated independently.
    event_records = [
        s
        for s in sources.values()
        if isinstance(s.get("body", {}).get("items"), list)
        and all("at" in e and "amount_fen" in e for e in s["body"]["items"])
    ]
    stress = next((s for s in sources.values() if "income_delay_calendar_days" in s.get("body", {})), None)
    for source in event_records:
        for scenario in ("baseline", "stress"):
            condition = stress["body"] if scenario == "stress" and stress else {}
            events = []
            for item in source["body"]["items"]:
                at = datetime.fromisoformat(item["at"])
                amount = item["amount_fen"]
                kind = item["kind"]
                if kind == "expected_income":
                    at += timedelta(days=condition.get("income_delay_calendar_days", 0))
                    delta = amount
                else:
                    pct = (
                        condition.get("subscription_increase_pct", 0)
                        if kind == "subscription"
                        else condition.get("other_expenses_increase_pct", 0)
                    )
                    delta = -int(
                        (Decimal(amount) * (100 + Decimal(pct)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                    )
                events.append((at, delta, kind))
            cash = available - new_reserve
            minimum = cash
            rows = []
            gap_start = None
            recovered = None
            for at, delta, kind in sorted(events, key=lambda x: (x[0], x[1] > 0)):
                cash += delta
                minimum = min(minimum, cash)
                if cash < 0 and gap_start is None:
                    gap_start = at
                if gap_start and cash >= 0 and recovered is None:
                    recovered = at
                rows.append(
                    {"at": at.isoformat(), "kind": kind, "delta_yuan": yuan(delta), "projected_cash_yuan": yuan(cash)}
                )
            facts.append(
                {
                    "kind": "timestamp_cashflow",
                    "scenario": scenario,
                    "evidence_ids": [source["source_id"], account["id"]] + ([stress["source_id"]] if stress else []),
                    "events": rows,
                    "ending_yuan": yuan(cash),
                    "maximum_funding_gap_yuan": yuan(max(0, -minimum)),
                    "first_gap_at": gap_start.isoformat() if gap_start else None,
                    "conditional_recovery_at": recovered.isoformat() if recovered else None,
                    "gap_hours": str(Decimal(str((recovered - gap_start).total_seconds())) / 3600)
                    if gap_start and recovered
                    else None,
                    "rule": "预计工资仅在相应情景的到账时刻计入；负数是资金需求，不是自动透支。",
                }
            )
    # Position, NAV and applicable fee are joined by product ID, never its name.
    grouped = {}
    for source in sources.values():
        product = source.get("body", {}).get("product_id")
        if product:
            grouped.setdefault(product, []).append(source)
    for product, records in grouped.items():
        position = next((s for s in records if "shares" in s["body"]), None)
        nav = next((s for s in records if "nav" in s["body"]), None)
        terms = next((s for s in records if "redemption_fee_bps" in s["body"]), None)
        if not (position and nav and terms):
            continue
        shares = Decimal(position["body"]["shares"])
        price = Decimal(nav["body"]["nav"])
        fee_rate = Decimal(terms["body"]["redemption_fee_bps"]) / 10000
        gross = int((shares * price * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        fee = int((gross * fee_rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        target = re.search(r"至少\s*(\d+(?:\.\d+)?)\s*元", query)
        fact = {
            "kind": "existing_position_redemption",
            "product_id": product,
            "evidence_ids": [s["source_id"] for s in [position, nav, terms]],
            "shares": str(shares),
            "nav": str(price),
            "nav_date": nav["body"].get("nav_date"),
            "gross_yuan": yuan(gross),
            "fee_yuan": yuan(fee),
            "net_yuan": yuan(gross - fee),
            "fee_pct": str(fee_rate * 100),
            "unlock_at": position["body"].get("unlock_at"),
            "rule": "仅为当前净值估算，不保证未来净额；新申购起点不能否定既有持仓赎回。",
        }
        if target:
            amount = Decimal(target[1])
            fact.update(
                target_yuan=str(amount),
                surplus_yuan=str(Decimal(gross - fee) / 100 - amount),
                minimum_nav_for_target=str(
                    (amount / (shares * (1 - fee_rate))).quantize(Decimal(".0001"), rounding=ROUND_HALF_UP)
                ),
            )
        facts.append(fact)
    return facts
