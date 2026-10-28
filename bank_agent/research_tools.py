"""Permission-scoped, read-only research evidence and deterministic financial checks.

No tool in this broker issues grants, invokes financial writes, or reads credentials.
The model selects questions and evidence; the verifier calculates financial claims.
"""

from datetime import date, datetime, timedelta
from contextlib import nullcontext
from decimal import Decimal
import hashlib

from sqlalchemy import select

from .errors import BankError
from .product_disclosures import disclosure_clauses
from .banking_calendar import settlement_date_from_settings
from .operations import DEFINITIONS, available, business_now, primary_account
from .storage import Document, Node, Plan, Transaction, User, canonical, documents, save_document


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def yuan(value):
    return f"{Decimal(value) / 100:,.2f}元"


PRODUCT_FIELDS = ("risk_level", "minimum_fen", "lock_days", "redeem_business_days", "fee_bps", "nav_1e4")
READS = {
    "bills": {"constraints", "account", "bills.report", "bills.anomalies", "cashflow.project", "transaction_details", "stress"},
    "wealth": {"constraints", "account", "wealth.compare", "cashflow.project", "product_terms"},
}
GREEN_QUERIES = {"bills.report", "bills.anomalies", "cashflow.project", "wealth.compare"}


def ensure_disclosures(db):
    """Add local synthetic disclosures to an existing database without resetting it.

    A changed catalog does NOT silently rewrite an existing disclosure. The verifier
    will detect mismatching versions until both sources are deliberately updated.
    """
    with db.write() as session:
        for doc in documents(session, "product", "system"):
            key = "disclosure_" + doc.id
            if session.get(Document, key):
                continue
            p = doc.data
            fields = {k: p[k] for k in PRODUCT_FIELDS if k in p}
            clauses = disclosure_clauses(p)
            save_document(session, key, "product_disclosure", "system", {
                "product_id": doc.id, "name": p["name"], "fields": fields,
                "clauses": clauses, "source": "local_synthetic_product_disclosure", "version": 1,
            })


class ResearchTools:
    def __init__(self, db, bank):
        self.db, self.bank = db, bank

    def fingerprint(self, user_id):
        # Hash all relevant owned state for freshness; never return other users' data.
        with self.db.read() as session:
            return self._fingerprint(session, user_id)

    @staticmethod
    def _fingerprint(session, user_id):
        u = session.get(User, user_id)
        account = primary_account(session, user_id)
        owned = [d for d in session.scalars(select(Document).where(Document.owner_id == user_id))
                 if d.kind in {"hold", "subscription", "expected_income", "holding", "redemption", "financial_evidence"}]
        catalog = [d for d in session.scalars(select(Document).where(Document.owner_id == "system"))
                   if d.kind in {"product", "product_disclosure", "banking_calendar"}]
        plans = list(session.scalars(select(Plan).where(Plan.user_id == user_id)))
        keys = [p.id for p in plans]
        nodes = list(session.scalars(select(Node).where(Node.plan_id.in_(keys)))) if keys else []
        rows = list(session.scalars(select(Transaction).where(Transaction.owner_id == user_id)))
        return digest({
            "clock": business_now(session).isoformat(), "balance": account.balance_fen,
            "risk": [u.risk_level, u.risk_valid_until],
            "docs": sorted([(d.id, d.data) for d in [*owned, *catalog]]),
            "transactions": sorted([(r.id, r.kind, r.amount_fen, r.posted_at, r.merchant_id) for r in rows]),
            "pending": sorted([(n.id, n.status, n.params, n.scheduled_for) for n in nodes
                               if n.operation in {"funds.transfer", "wealth.purchase"}
                               and n.status in {"READY", "WAIT_CONFIRM", "WAIT_MFA", "WAIT_TIME", "UNKNOWN"}]),
        })

    def read(self, user_id, role, name, brief, session=None, exclude_plan_id=None):
        if name not in READS.get(role, set()):
            raise BankError("RESEARCH_TOOL_DENIED", "研究仅能查询本领域已授权的只读资料", 403)
        with nullcontext(session) if session is not None else self.db.read() as session:
            clock = business_now(session)
            user = session.get(User, user_id)
            if not user:
                raise BankError("UNAUTHENTICATED", "请先登录", 401)
            period = brief.period or clock.strftime("%Y-%m")
            if name == "constraints":
                value = brief.model_dump(mode="json")
                sources = ["user_declared_research_constraints"]
            elif name == "account":
                a = primary_account(session, user_id)
                value = {"balance_fen": a.balance_fen, "available_fen": available(session, user_id),
                         "risk_level": user.risk_level, "risk_valid_until": user.risk_valid_until,
                         "supporting_record_index": [{k: v for k, v in d.data.items()
                             if k not in {"body", "related_source_ids", "status", "visibility"}}
                             for d in documents(session, "financial_evidence", user_id)],
                         "banking_calendar": (session.get(Document, "banking_calendar").data
                             if session.get(Document, "banking_calendar") else {})}
                sources = [a.id] + [d.id for d in documents(session, "financial_evidence", user_id)]
            elif name in GREEN_QUERIES or name == "stress":
                op = "cashflow.project" if name == "stress" else name
                # Explicit allowlist plus independent tier check, never arbitrary dispatch.
                if op not in GREEN_QUERIES or DEFINITIONS[op][2] != "GREEN":
                    raise BankError("RESEARCH_TOOL_DENIED", "该查询需要经过银行授权流程", 403)
                params = ({"period": period, "annual": len(period) == 4} if op == "bills.report" else
                          {"days": brief.days, "income_delay_days": 7 if name == "stress" else 0,
                           "expense_increase_pct": 20 if name == "stress" else 0}
                          if op == "cashflow.project" else {})
                value = self.bank.dispatch(session, user_id, op, self.bank.validate_params(op, params), "research_read")
                sources = value.get("transaction_ids", [op])
                if op == "cashflow.project":
                    # Existing plans are contingent obligations, not posted expenses.
                    plan_ids = list(session.scalars(select(Plan.id).where(Plan.user_id == user_id, Plan.id != (exclude_plan_id or ""))))
                    horizon = clock.date() + timedelta(days=brief.days)
                    pending = []
                    for n in session.scalars(select(Node).where(Node.plan_id.in_(plan_ids))):
                        if n.operation not in {"funds.transfer", "wealth.purchase"} or n.status not in {
                            "READY", "WAIT_CONFIRM", "WAIT_MFA", "WAIT_TIME", "UNKNOWN"
                        }:
                            continue
                        if n.scheduled_for and datetime.fromisoformat(n.scheduled_for).date() > horizon:
                            continue
                        amount = n.params.get("amount_fen")
                        if isinstance(amount, int) and not isinstance(amount, bool):
                            pending.append({"source": n.id, "amount_fen": amount, "status": n.status,
                                            "date": n.scheduled_for, "contingent": True,
                                            'operation': n.operation, 'recipient_hint': n.params.get('recipient_hint')})
                    value = {**value, "pending_plans": pending}
            elif name == "transaction_details":
                # Read the full anomaly source, including historical records outside the report period.
                rows = list(session.scalars(select(Transaction).where(Transaction.owner_id == user_id)
                                           .order_by(Transaction.posted_at, Transaction.id)))
                value = {"rows": [{"id": r.id, "kind": r.kind, "amount_fen": r.amount_fen,
                                   "merchant_id": r.merchant_id, "subject": r.subject, "category": r.category,
                                   "posted_at": r.posted_at, "receipt_id": r.receipt_id} for r in rows[:2000]],
                         "complete": len(rows) <= 2000, "total_count": len(rows)}
                sources = [r.id for r in rows[:2000]]
            else:
                value = {"products": []}
                sources = []
                for p in documents(session, "product", "system"):
                    d = session.get(Document, "disclosure_" + p.id)
                    value["products"].append({"catalog": p.data,
                        "disclosure": d.data if d and d.kind == "product_disclosure" and d.owner_id == "system" else None})
                    sources.extend([p.id, "disclosure_" + p.id])
            # Detail tools, rather than the account/index query, reveal the raw
            # owner-scoped corroborating records. This preserves actual follow-up
            # retrieval and treats missing/superseded source status as evidence.
            if name in {"transaction_details", "stress", "product_terms"}:
                records = documents(session, "financial_evidence", user_id)
                value = {**value, "supporting_records": [d.data for d in records]}
                sources = [*sources, *[d.id for d in records]]
            h = digest(value)
            return {"id": "ev_" + digest([role, name, h])[:20], "role": role, "tool": name,
                    "source_ids": sources, "as_of": clock.isoformat(), "sha256": h, "version": h,
                    "authority": "user_declared" if name == "constraints" else
                    "bank_read" if name != "product_terms" else "synthetic_disclosure",
                    "data": value}


def derive(role, evidence, brief):
    """Reproducible, source-bound claim library, recomputed by the verifier."""
    by_tool = {e["tool"]: e for e in evidence.values() if e["role"] == role}
    claims, missing, products, options = [], [], [], []

    def claim(key, text, tools, values=None, kind="fact"):
        refs = [by_tool[t]["id"] for t in tools if t in by_tool]
        claims.append({"id": role + ":" + key, "text": text, "evidence_ids": refs,
                       "values": values or {}, "kind": kind})

    a = by_tool["account"]["data"]
    cf = by_tool["cashflow.project"]["data"]
    if any(p["status"] == "UNKNOWN" for p in cf["pending_plans"]):
        missing.append("已有提交结果尚未核实，请先查询原银行回执，再生成资金办理方案。")
    as_of = date.fromisoformat(by_tool["account"]["as_of"][:10])
    horizon = as_of + timedelta(days=brief.days)
    declared = [x for x in brief.expenses if as_of <= date.fromisoformat(x.date) <= horizon]
    declared_total = sum(x.amount_fen for x in declared)
    # Reserving cash and paying the same declared expense are separate inputs; UI explains this.
    reserve = brief.reserve_fen + brief.emergency_fen
    pending = sum(p["amount_fen"] for p in cf["pending_plans"])
    known = cf["known_future_debits_fen"]
    room = max(0, a["available_fen"] - known - declared_total - reserve - pending)
    no_income_gap = max(0, known + declared_total + reserve + pending - a["available_fen"])
    funding = {"available_fen": a["available_fen"], "known_debits_fen": known,
               "declared_expenses_fen": declared_total, "new_reserve_fen": reserve,
               "contingent_plan_fen": pending, "investable_ceiling_fen": room,
               "shortfall_without_income_fen": no_income_gap}
    claim("funding", f"当前可用{yuan(a['available_fen'])}；未来{brief.days}天已知扣费{yuan(known)}，"
          f"你补充的支出{yuan(declared_total)}，新增生活与应急预留{yuan(reserve)}，"
          f"未执行计划的潜在支出{yuan(pending)}。不依赖预计收入时，剩余资金上限{yuan(room)}，缺口{yuan(no_income_gap)}。",
          ["account", "cashflow.project", "constraints"], funding, "scenario")
    claim("coverage", "测算覆盖已登记代扣、你确认的预计收入、你补充的支出与未执行计划。未登记的房租、还款等未自动计入；未来收入与待到账赎回不能用于当前申购。",
          ["cashflow.project"], {"unposted_income_counted_as_available": False}, "limitation")
    if role == "bills":
        if cf["obligations"]:
            detail = "；".join(f"{x['date']} {x.get('name', '已登记订阅')} {yuan(x['amount_fen'])}" for x in cf["obligations"])
            claim("obligations", "未来已登记扣费明细：" + detail + f"，合计{yuan(known)}。", ["cashflow.project"])
        report = by_tool["bills.report"]["data"]
        claim("ledger", f"{report['period']}账本记录：工资收入{yuan(report['income_fen'])}，账户转入{yuan(report['incoming_fen'])}，"
              f"消费净额{yuan(report['consumption_fen'])}（已扣除退款{yuan(report['refund_fen'])}），转账及AA支出{yuan(report['transfer_fen'])}。"
              "历史收入不等于当前余额，转账不计作消费。", ["bills.report"], report)
        if not report["transaction_ids"]:
            claim("empty", "该期间暂无入账流水；这不能证明没有未来支出义务。", ["bills.report"], kind="limitation")
        rows_ev = by_tool.get("transaction_details")
        anomalies = by_tool["bills.anomalies"]["data"]["alerts"]
        if rows_ev:
            rows = {r["id"]: r for r in rows_ev["data"]["rows"]}
            if not rows_ev["data"]["complete"]:
                missing.append("流水超过本次核查上限，部分原始记录未完成核验。")
            for i, alert in enumerate(anomalies[:20]):
                matching = [rows.get(k) for k in alert["transaction_ids"]]
                if any(r is None for r in matching):
                    missing.append("疑似异常扣款缺少原始流水，需要银行人员核查。")
                    continue
                r = matching[0]
                if len({(x["merchant_id"], x["posted_at"][:10], x["amount_fen"], x["kind"]) for x in matching}) != 1:
                    missing.append("异常分组与原始流水冲突，尚不能确认原因。")
                    continue
                claim(f"anomaly_{i}", f"{r['posted_at'][:10]}，{r['subject']}，每笔{yuan(r['amount_fen'])}，"
                      f"发现{len(matching)}笔同商户、同日、同金额消费，流水引用：{'、'.join(alert['transaction_ids'])}。可能是重复扣费，也可能是多次购买；需核对商户订单，不能据此认定欺诈或自动退款。",
                      ["bills.anomalies", "transaction_details"], {"transaction_ids": alert["transaction_ids"],
                      "classification": "suspected_not_confirmed_fraud"}, "suspected")
            if not anomalies:
                claim("anomaly_none", "本次同商户、同日、同金额规则未发现疑似重复扣款；该规则不能排除其他异常。",
                      ["bills.anomalies", "transaction_details"], kind="limitation")
        else:
            missing.append("需要回读原始流水核验异常分组。")
        if "stress" in by_tool:
            s = by_tool["stress"]["data"]
            # Include user expenses and reserves chronologically in the forecast; income stays indicative.
            events = [(x["date"], -(x["amount_fen"] * 120 // 100)) for x in s["obligations"]]
            events += [(x.date, -x.amount_fen) for x in declared]
            events += [(x["date"], x["amount_fen"]) for x in s["indicative_income"]]
            projection = a["available_fen"] - reserve - pending
            minimum = projection
            for _, delta in sorted(events, key=lambda e: (e[0], e[1] > 0)):
                projection += delta
                minimum = min(minimum, projection)
            stressed_total = sum(x["amount_fen"] * 120 // 100 for x in s["obligations"])
            claim("stress", f"压力情景：预计收入推迟7天、已知代扣增加20%，原扣费{yuan(known)}，压力扣费合计{yuan(stressed_total)}（每笔增加20%后按分取整）。计入你的支出与预留后，保守资金缺口为{yuan(max(0, -minimum))}。"
                  "这是条件测算，预计收入不会变成当前可用余额。可选择补足资金、调整可选预算，或向商户核对续费日期与必要性；涉及变更须另行核对确认，本次未转账或取消订阅。", ["stress", "account", "constraints"],
                  {"conservative_shortfall_fen": max(0, -minimum), "income_delay_days": 7,
                   "expense_increase_pct": 20}, "scenario")
        else:
            missing.append("需要补做收入延迟与扣费增加的压力测试。")
        if reserve and reserve <= a["available_fen"] and not brief.analysis_only:
            options.append({"id": "reserve", "label": f"预留生活与应急资金{yuan(reserve)}", "kind": "reserve",
                            "amount_fen": reserve, "evidence_ids": [by_tool["account"]["id"]]})
    else:
        terms = by_tool.get("product_terms")
        if not terms:
            missing.append("需要阅读完整产品条款，不能只凭产品名称推荐。")
        else:
            for entry in terms["data"]["products"]:
                p, d = entry["catalog"], entry["disclosure"]
                reasons, notes = [], []
                fields = {k: p.get(k) for k in PRODUCT_FIELDS}
                valid_terms = (d is not None and d.get("fields") == fields
                               and d.get("clauses") == disclosure_clauses(p))
                if not valid_terms:
                    reasons.append("产品条款缺失或与当前产品目录冲突，不能生成申购方案")
                if a["risk_valid_until"] < as_of.isoformat():
                    reasons.append("你的风险测评已过期，需要先重新测评")
                if p["risk_level"] > a["risk_level"]:
                    reasons.append(f"产品风险R{p['risk_level']}高于你的测评R{a['risk_level']}")
                amount = brief.amount_fen
                if room < p["minimum_fen"]:
                    reasons.append(f"扣除义务与预留后资金不足起购金额{yuan(p['minimum_fen'])}")
                if amount is not None and amount < p["minimum_fen"]:
                    reasons.append(f"计划申购金额低于起购金额{yuan(p['minimum_fen'])}")
                if amount is not None and amount > room:
                    reasons.append(f"计划申购金额超过测算资金上限{yuan(room)}")
                unlocks = as_of + timedelta(days=p["lock_days"])
                conservative_clock = datetime.fromisoformat(unlocks.isoformat() + "T15:59:00+08:00")
                settlement = settlement_date_from_settings(conservative_clock, p["redeem_business_days"],
                    a.get("banking_calendar", {}), p.get("cutoff_hour", 15))
                if brief.needed_on and settlement > date.fromisoformat(brief.needed_on):
                    reasons.append(f"按锁定期和15:00后赎回情景，预计{settlement.isoformat()}到账，晚于用款日{brief.needed_on}")
                if brief.forbid_purchase:
                    reasons.append("你明确要求不申购理财")
                if p["id"] in brief.excluded_product_ids:
                    reasons.append("你明确要求排除这款产品")
                notes.append("T+n采用可配置营业日的演示日历，未接入官方节假日数据；实际到账以执行回执为准")
                if amount is None:
                    notes.append("尚未指定申购金额；当前只比较条款，不生成申购计划")
                if brief.needed_on is None:
                    notes.append("未提供用款日期，尚未判断是否满足你的具体用款时间")
                item = {"product_id": p["id"], "name": p["name"], **fields, "excluded": bool(reasons),
                        "reasons": reasons, "notes": notes, "conservative_settles_on": settlement.isoformat(),
                        "evidence_ids": [terms["id"], by_tool["account"]["id"], by_tool["cashflow.project"]["id"]]}
                products.append(item)
                text = f"{p['name']}：风险R{p['risk_level']}，起购{yuan(p['minimum_fen'])}，锁定{p['lock_days']}天，赎回T+{p['redeem_business_days']}，费率{Decimal(p['fee_bps']) / 100}%。"
                text += "排除原因：" + "；".join(reasons) if reasons else "符合本次已提供的风险与资金条件。"
                claim("product_" + p["id"], text, ["product_terms", "account", "cashflow.project"], item, "comparison")
                if not reasons and amount is not None and not brief.analysis_only:
                    options.append({"id": "buy_" + p["id"], "label": f"申购{p['name']} {yuan(amount)}",
                                    "kind": "purchase", "product_id": p["id"], "amount_fen": amount,
                                    "reserve_fen": reserve, "evidence_ids": item["evidence_ids"]})
            products.sort(key=lambda p: (p["excluded"], p["lock_days"], p["redeem_business_days"], p["risk_level"]))
        claim("no_yield", "比较依据为适当性、资金和流动性条件。当前没有可核验的收益预测，不以产品名称或历史净值承诺收益。",
              ["wealth.compare"], kind="limitation")
    return {"role": role, "claims": claims, "missing": missing, "products": products, "options": options,
            "funding": funding}


def guard_research_purchase(session, bank, plan, node, params):
    """Recheck research conditions in the SAME locked transaction as authorization/execution."""
    origin = plan.review.get("research")
    if not origin or node.operation != "wealth.purchase":
        return
    from .research_schemas import ResearchBrief
    source = session.get(Document, origin["id"])
    if not source or source.kind != "research" or source.owner_id != plan.user_id:
        raise BankError("RESEARCH_SOURCE_INVALID", "原研究方案不可用，请重新分析")
    data = source.data
    option = next((o for o in data["report"]["options"] if o["id"] == origin["option_id"]), None)
    if not option or option.get("product_id") != params["product_id"] or option["amount_fen"] != params["amount_fen"]:
        raise BankError("RESEARCH_PLAN_CHANGED", "申购安排与研究方案不同，请重新分析")
    brief = ResearchBrief.model_validate(data["report"]["constraints"])
    if brief.analysis_only or brief.forbid_purchase:
        raise BankError("RESEARCH_NEGATION", "已保留你不申购的要求")
    # The reserve step has already reduced available cash; never subtract it twice.
    brief.reserve_fen = brief.emergency_fen = 0
    if brief.needed_on and brief.needed_on < business_now(session).date().isoformat():
        raise BankError("RESEARCH_CONDITIONS_CHANGED", "用款日期已过去，请重新分析")
    broker = ResearchTools(bank.db, bank)
    evidence = {}
    for tool in ["constraints", "account", "wealth.compare", "cashflow.project", "product_terms"]:
        ev = broker.read(plan.user_id, "wealth", tool, brief, session=session, exclude_plan_id=plan.id)
        evidence[ev["id"]] = ev
    result = derive("wealth", evidence, brief)
    product = next((p for p in result["products"] if p["product_id"] == params["product_id"]), None)
    if not product or product["excluded"]:
        reason = "；".join(product["reasons"]) if product else "产品资料缺失"
        raise BankError("RESEARCH_CONDITIONS_CHANGED", "当前条件已不满足原研究方案：" + reason)
