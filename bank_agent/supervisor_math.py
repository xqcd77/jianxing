"""Read-only calculations for the current Supervisor; no oracle or bank writes."""

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING
import re

from pydantic import Field, StrictInt

from .research_math import verified_financial_facts, yuan
from .schemas import StrictModel


class CashAlternative(StrictModel):
    name: str = Field(min_length=1, max_length=30)
    retained_cash_fen: StrictInt = Field(ge=0, le=100_000_000)
    purchase_fen: StrictInt = Field(ge=0, le=100_000_000)


class ResearchDetails(StrictModel):
    needed_at: str | None = Field(default=None, max_length=40)
    income_delay_days: StrictInt = Field(default=0, ge=0, le=366)
    alternatives: list[CashAlternative] = Field(default_factory=list, max_length=4)


def source_records(state):
    return {s["source_id"]: s for e in state["evidence"].values() for s in e["data"].get("supporting_records", [])}


def financial_facts(state):
    facts = verified_financial_facts(state, deduplicate_expenses=True)
    sources = source_records(state)
    account = next(e for e in state["evidence"].values() if e["tool"] == "account")
    details = ResearchDetails.model_validate(state.get("research_details", {}))
    now = datetime.fromisoformat(account["as_of"]) if account.get("as_of") else None
    deadline = datetime.fromisoformat(details.needed_at) if details.needed_at else None
    # This date alone does not imply a specific hour. Never invent one.
    calendar_source = next((s for s in sources.values() if "business_dates" in s.get("body", {})), None)
    calendar = account["data"].get("banking_calendar", {})

    def open_day(day):
        if calendar_source:
            return day.isoformat() in calendar_source["body"]["business_dates"]
        if day.isoformat() in calendar.get("closed_dates", []):
            return False
        return day.isoformat() in calendar.get("open_dates", []) or day.weekday() < 5

    linked = {}
    for index in sources.values():
        for label, item in index.get("body", {}).items():
            if isinstance(item, dict) and item.get("terms_id") in sources and isinstance(item.get("value_fen"), int):
                linked[item["terms_id"]] = {"index_id": index["source_id"], "label": label, **item}
    for source in sources.values():
        body = source.get("body", {})
        holding = linked.get(source["source_id"])
        identity = body.get("product_id") or (source["source_id"] if holding else None)
        cutoff = body.get("cutoff_exclusive")
        settlement_time = body.get("settlement_time", body.get("same_business_day_settlement_time"))
        days = body.get(
            "settlement_business_days",
            body.get(
                "settlement_business_days_after_acceptance",
                0 if body.get("same_business_day_settlement_time") else None,
            ),
        )
        if not (now and identity and cutoff and settlement_time and isinstance(days, int)):
            continue
        position = next(
            (
                s
                for s in sources.values()
                if s.get("body", {}).get("product_id") == identity and s["body"].get("unlock_at")
            ),
            None,
        )
        earliest = max(now, datetime.fromisoformat(position["body"]["unlock_at"])) if position else now

        def settlement(application):
            day = application.date()
            after_cutoff = application.timetz().replace(tzinfo=None) >= time.fromisoformat(cutoff)
            if not open_day(day) or after_cutoff:
                day += timedelta(days=1)
                for _ in range(367):
                    if open_day(day):
                        break
                    day += timedelta(days=1)
                else:
                    return None
            for _ in range(days):
                for _ in range(367):
                    day += timedelta(days=1)
                    if open_day(day):
                        break
                else:
                    return None
            return datetime.combine(day, time.fromisoformat(settlement_time), tzinfo=now.tzinfo)

        arrival = settlement(earliest)
        latest = latest_arrival = None
        if deadline:
            # Backward search is bounded and uses strict exclusive cutoff.
            for offset in range(min(366, max(0, (deadline.date() - now.date()).days)) + 1):
                day = deadline.date() - timedelta(days=offset)
                if not open_day(day):
                    continue
                boundary = datetime.combine(day, time.fromisoformat(cutoff), tzinfo=now.tzinfo)
                application = boundary - timedelta(microseconds=1)
                result = settlement(application)
                if application >= earliest and result and result <= deadline:
                    latest, latest_arrival = boundary, result
                    break
        facts.append(
            {
                "kind": "redemption_timing",
                "product_id": identity,
                "source_title": source.get("title", identity),
                "evidence_ids": [source["source_id"], account["id"]]
                + ([position["source_id"]] if position else [])
                + ([calendar_source["source_id"]] if calendar_source else [])
                + ([holding["index_id"]] if holding else []),
                "as_of": now.isoformat(),
                "cutoff_exclusive": cutoff,
                "settlement_business_days": days,
                "linked_holding_value_yuan": yuan(holding["value_fen"]) if holding else None,
                "amount_available_by_deadline_yuan": yuan(
                    min(holding["value_fen"], body.get("remaining_daily_limit_fen", holding["value_fen"]))
                )
                if holding and arrival and deadline and arrival <= deadline and body.get("fee_bps") == 0
                else "0"
                if holding and arrival and deadline and arrival > deadline
                else None,
                "earliest_apply_at": earliest.isoformat(),
                "earliest_arrival_at": arrival.isoformat() if arrival else None,
                "deadline_at": deadline.isoformat() if deadline else None,
                "latest_apply_before": latest.isoformat() if latest else None,
                "latest_arrival_at": latest_arrival.isoformat() if latest_arrival else None,
                "meets_deadline": arrival <= deadline if arrival and deadline else None,
                "new_purchase_minimum_yuan": yuan(body["new_purchase_minimum_fen"])
                if "new_purchase_minimum_fen" in body
                else None,
                "partial_redemption_allowed": body.get("partial_redemption_allowed"),
                "minimum_partial_redemption_yuan": yuan(body["minimum_partial_redemption_fen"])
                if "minimum_partial_redemption_fen" in body
                else None,
                "rule": "截止时刻严格不含；到账必须早于用款时刻，预计到账不等于当前余额。新申购门槛不适用于已有份额赎回。",
            }
        )
    linked_facts = [f for f in facts if f["kind"] == "redemption_timing" and f.get("linked_holding_value_yuan")]
    target = re.search(r"(?:要用|需要|用到|至少|用款)[^，。；\d]{0,6}(\d+(?:\.\d+)?)\s*元", state["query"])
    if linked_facts and all(f["amount_available_by_deadline_yuan"] is not None for f in linked_facts) and target:
        at_deadline = Decimal(yuan(account["data"]["available_fen"])) + sum(
            Decimal(f["amount_available_by_deadline_yuan"]) for f in linked_facts
        )
        facts.append(
            {
                "kind": "deadline_liquidity",
                "evidence_ids": list(dict.fromkeys(r for f in linked_facts for r in f["evidence_ids"])),
                "target_yuan": target[1],
                "deadline_at": details.needed_at,
                "maximum_available_yuan": str(at_deadline),
                "gap_yuan": str(max(Decimal(0), Decimal(target[1]) - at_deadline)),
                "rule": "只把按当前受理时刻申请且能按期到账的已核查持仓计入；不计预计工资，不自动赎回。",
            }
        )
    for fact in facts:
        if fact["kind"] == "existing_position_redemption" and fact.get("target_yuan"):
            # Round UP: a rounded-down threshold cannot fund the target.
            shares, fee = Decimal(fact["shares"]), Decimal(fact["fee_pct"]) / 100
            fact["minimum_nav_for_target"] = str(
                (Decimal(fact["target_yuan"]) / (shares * (1 - fee))).quantize(Decimal(".0001"), rounding=ROUND_CEILING)
            )
    # Each counterfactual gets a fresh cash balance; alternatives never add together.
    for source in sources.values():
        body = source.get("body", {})
        if not all(k in body for k in ("salary_expected_at", "salary_fen", "payment_at", "payment_fen")):
            continue
        payment_at = datetime.fromisoformat(body["payment_at"])
        for alternative in details.alternatives:
            if alternative.retained_cash_fen + alternative.purchase_fen != account["data"]["available_fen"]:
                continue  # An incomplete allocation needs clarification, not invented residuals.
            for label, delay in [("按时", 0), ("延期", details.income_delay_days)]:
                income_at = datetime.fromisoformat(body["salary_expected_at"]) + timedelta(days=delay)
                before = alternative.retained_cash_fen + (body["salary_fen"] if income_at < payment_at else 0)
                after = before - body["payment_fen"]
                facts.append(
                    {
                        "kind": "alternative_cashflow",
                        "name": alternative.name,
                        "scenario": label,
                        "evidence_ids": [source["source_id"], account["id"]],
                        "retained_cash_yuan": yuan(alternative.retained_cash_fen),
                        "purchase_yuan": yuan(alternative.purchase_fen),
                        "income_at": income_at.isoformat(),
                        "payment_at": payment_at.isoformat(),
                        "cash_before_payment_yuan": yuan(before),
                        "cash_after_payment_yuan": yuan(after),
                        "funding_gap_yuan": yuan(max(0, -after)),
                        "rule": "互斥方案分别核算；预计工资只在该情景到账后计入，不是当前可用。产品净值不保证，不假设赎回自动到账。",
                    }
                )
    return facts


def checked_details(raw, query, needed_on):
    """Reject invented amounts/dates before using model-extracted scenarios."""
    details = ResearchDetails.model_validate(raw)
    numbers = {int(Decimal(m.group()) * 100) for m in re.finditer(r"\d+(?:\.\d+)?", query)}
    for alternative in details.alternatives:
        if any(n not in numbers for n in (alternative.retained_cash_fen, alternative.purchase_fen)):
            raise ValueError("ungrounded_alternative_amount")
        if alternative.name not in query:
            raise ValueError("ungrounded_alternative_name")
    if details.needed_at:
        at = datetime.fromisoformat(details.needed_at)
        if not needed_on or at.date().isoformat() != needed_on:
            raise ValueError("ungrounded_deadline_date")
        stated_times = set()
        for match in re.finditer(r"(?<!\d)(\d{1,2})\s*(?:点|时|:|：)\s*(?:(\d{1,2})\s*分?|(半))?", query):
            stated_times.add((int(match[1]), 30 if match[3] else int(match[2] or 0)))
        if (at.hour, at.minute) not in stated_times or at.second or at.microsecond:
            raise ValueError("ungrounded_deadline_time")
        if not at.tzinfo:
            raise ValueError("deadline_requires_timezone")
    if details.income_delay_days and not re.search(
        rf"(?:延期|推迟|晚到|延迟)\s*{details.income_delay_days}\s*天", query
    ):
        raise ValueError("ungrounded_income_delay")
    return details.model_dump(mode="json")


def fact_claims(state):
    """User-facing, deterministic arithmetic; no approval or trading advice."""
    facts = financial_facts(state)
    aliases = {e["id"]: e["id"] for e in state["evidence"].values()}
    aliases.update(
        {s["source_id"]: e["id"] for e in state["evidence"].values() for s in e["data"].get("supporting_records", [])}
    )
    claims = []

    def add(text, fact):
        refs = list(dict.fromkeys(aliases[r] for r in fact["evidence_ids"] if r in aliases))
        claims.append(
            {
                "id": f"bills:computed_{len(claims)}",
                "kind": "analysis",
                "text": text,
                "evidence_ids": refs,
                "values": {},
            }
        )

    alternatives = [f for f in facts if f["kind"] == "alternative_cashflow"]
    for f in facts:
        if (
            f["kind"] == "unified_budget"
            and not alternatives
            and any(
                Decimal(f[k]) for k in ("new_reserve_yuan", "requested_investment_yuan", "new_proposed_transfer_yuan")
            )
        ):
            text = (
                f"整份安排：生活及应急预留{f['new_reserve_yuan']}元、拟转账{f['new_proposed_transfer_yuan']}元、"
                f"拟申购{f['requested_investment_yuan']}元，另含已登记义务与其他支出。总需{f['whole_plan_required_yuan']}元，"
                f"当前可用{f['available_yuan']}元，资金缺口{f['whole_plan_gap_yuan']}元。"
            )
            if Decimal(f["whole_plan_gap_yuan"]) > 0:
                text += (
                    f"不能按原金额整份执行。若保留生活预留及转账目标，可考虑把拟投金额调至最多"
                    f"{f['maximum_new_investment_yuan']}元，或补足缺口；这只是待你选择的候选，不会擅自改金额或部分执行。"
                    "条件变化后原确认不可沿用，须重新核对并授权。"
                )
            add(text + "预计收入不计入当前可用，本次分析未办理资金操作。", f)
        elif f["kind"] == "existing_position_redemption":
            text = (
                f"本人已有持仓{f['shares']}份，按{f['nav_date']}净值{f['nav']}元/份估算："
                f"毛额{f['gross_yuan']}元，赎回费率{f['fee_pct']}%，费用{f['fee_yuan']}元，净额{f['net_yuan']}元。"
            )
            if f.get("target_yuan"):
                text += (
                    f"相对目标{f['target_yuan']}元差额{f['surplus_yuan']}元；不计其他变化时净值至少约"
                    f"{f['minimum_nav_for_target']}元/份才能净得目标金额。"
                )
            add(text + "这是当前净值估算，不保证未来净额或本金。新申购门槛不否定既有份额赎回。", f)
        elif f["kind"] == "redemption_timing":
            text = (
                f"{f['source_title']}：本次业务时刻为{f['as_of']}，受理截止{f['cutoff_exclusive']}（不含），"
                f"受理后按已读取营业日历T+{f['settlement_business_days']}结算。按当前条件最早可申请{f['earliest_apply_at']}，预计到账{f['earliest_arrival_at']}。"
            )
            if f["deadline_at"]:
                text += f"用款时点为{f['deadline_at']}。"
                if f["latest_apply_before"]:
                    text += (
                        f"为赶上用款，最晚须在{f['latest_apply_before']}之前申请，预计{f['latest_arrival_at']}到账。"
                    )
                else:
                    text += "当前已核查时间范围内未找到可保证赶上用款的受理时点，需再核实。"
            if f["new_purchase_minimum_yuan"]:
                text += f"{f['new_purchase_minimum_yuan']}元为新申购起点，与已有持仓赎回分别判断。"
            add(text + "以上为条款时点估计，不是已提交或到账回执。", f)
        elif f["kind"] == "deadline_liquidity":
            add(
                f"按本次业务时刻申请、只计可在{f['deadline_at']}前到账的已核查赎回，连同当前活期最多可筹"
                f"{f['maximum_available_yuan']}元；相对用款{f['target_yuan']}元，仍缺{f['gap_yuan']}元。"
                "需补足缺口或重新安排用款，不能把未到账资产当现金。本次未提交赎回。",
                f,
            )
        elif f["kind"] == "alternative_cashflow":
            add(
                f"方案{f['name']}：保留现金{f['retained_cash_yuan']}元、拟买{f['purchase_yuan']}元；工资{f['scenario']}情景"
                f"于{f['income_at']}预计到账。在{f['payment_at']}付款前现金{f['cash_before_payment_yuan']}元，"
                f"付款后测算{f['cash_after_payment_yuan']}元，资金缺口{f['funding_gap_yuan']}元。"
                "负数表示资金需求，不是自动透支；未假设理财赎回已到账。",
                f,
            )
    if alternatives:
        worst = {}
        for f in alternatives:
            worst[f["name"]] = max(worst.get(f["name"], Decimal(0)), Decimal(f["funding_gap_yuan"]))
        best = min(worst, key=worst.get)
        if len(set(worst.values())) > 1:
            add(
                f"就已登记付款在工资延迟时的现金缺口而言，方案{best}更稳健；这不代表其投资收益更高或本金有保证。"
                "若缺口情景发生，应先降低拟投资金额或补足资金，重新比较后由你选择，不自动购买或转账。",
                alternatives[0],
            )
    return claims
