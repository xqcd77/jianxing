"""Whole-plan financial simulation using the SAME deterministic sandbox rules.

Every simulated write is rolled back to a savepoint. No adapter, grant, audit,
execution receipt or generated code is invoked by this service.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
from types import SimpleNamespace


from .dag import references
from .errors import BankError
from .failures import error_details
from .operations import available, business_now, daily_outgoing, held_total, primary_account, risk_tier
from .storage import Document, User, canonical, documents, save_document
from .risk import cashflow_pressure


def resolve(value, results):
    if isinstance(value, str) and value.startswith("$"):
        key, path = next(references(value))
        result = results.get(key)
        if result is None:
            raise BankError("DEPENDENCY_NOT_READY", "需要前置步骤的实际结果")
        for field in path.split("."):
            result = result[field]
        return result
    if isinstance(value, dict):
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    return value


class PlanSimulator:
    def __init__(self, db, bank):
        self.db, self.bank = db, bank

    def for_specs(self, user_id, specs, review):
        nodes = [SimpleNamespace(id=s.id, local_id=s.id, operation=s.operation, params=s.params,
                    depends_on=s.depends_on, scheduled_for=s.scheduled_for, status="READY", result=None) for s in specs]
        order = {key: i for i, key in enumerate(review["topological_order"])}
        nodes.sort(key=lambda n: order[n.local_id])
        with self.db.simulation() as session:
            return self.simulate(session, user_id, nodes, review.get("ordering", {}))

    def simulate(self, session, user_id, nodes, ordering=None):
        ordering = ordering or {}
        clock = business_now(session)
        user = session.get(User, user_id)
        def funds():
            return {"balance_fen": primary_account(session, user_id).balance_fen,
                    "reserved_fen": held_total(session, user_id), "available_fen": available(session, user_id)}
        before = funds()
        relevant = [d for d in documents(session, "product", "system")]
        relevant += [d for kind in ["contact", "hold", "holding", "redemption", "subscription", "card", "expected_income"]
                     for d in documents(session, kind, user_id)]
        source = {"account": before, "business_now": clock.isoformat(), "risk": [user.risk_level, user.risk_valid_until],
                  "daily_outgoing_fen": daily_outgoing(session, user_id),
                  "documents": sorted([(d.id, deepcopy(d.data)) for d in relevant])}
        keys = {n.local_id for n in nodes}
        baseline = [{"id": n.local_id, "operation": n.operation, "params": deepcopy(n.params),
                     "status": n.status, "scheduled_for": n.scheduled_for} for n in nodes]
        results = {n.local_id: deepcopy(n.result["data"]) for n in nodes if n.result and n.status in {"SUCCEEDED", "PARTIAL"}}
        steps, blockers, deferred = [], [], []
        immediate_after = before
        shadow_available = before["available_fen"]
        minimum_available = shadow_available
        has_future = False
        remaining = list(nodes)
        dates, ordered = {}, []
        while remaining:
            pending_ids = {n.local_id for n in remaining}
            def edges(n):
                return [*n.depends_on, *ordering.get(n.local_id, [])]
            ready = [n for n in remaining if not any(d in pending_ids for d in edges(n))]
            if not ready:
                raise BankError("INVALID_DAG", "无法模拟有环的业务计划")
            def when(n):
                base = datetime.fromisoformat(n.scheduled_for) if n.scheduled_for else clock
                return max([clock, base, *[dates.get(d, clock) for d in edges(n)]])
            n = min(ready, key=when)
            dates[n.local_id] = when(n)
            ordered.append(n)
            remaining.remove(n)
        recurring_debits, event_warnings = 0, []
        # Always rollback, even when a rule or a serialization fails mid-simulation.
        savepoint = session.begin_nested()
        try:
            obligations = self.bank.dispatch(session, user_id, "cashflow.project",
                self.bank.validate_params("cashflow.project", {"days": 30}), "simulation_cashflow")
            for n in ordered:
                if n.status in {"SUCCEEDED", "SKIPPED", "CANCELLED", "PARTIAL"}:
                    continue
                if n.status == "UNKNOWN":
                    blockers.append({"node_id": n.id, "code": "RESULT_UNKNOWN", "category": "technical",
                        "message": "已提交步骤的结果仍在核实，不能把它当作失败重做。", "next_step": "先核实提交结果。", "future": False})
                    continue
                future = dates[n.local_id] > clock
                has_future |= future
                if future:
                    event_before = available(session, user_id)
                    session.get(Document, "business_clock").data = {"now": dates[n.local_id].isoformat()}
                    session.flush()
                    extra = self._dated_events(session, user_id, dates[n.local_id].date(), event_warnings)
                    recurring_debits += extra
                    shadow_available += available(session, user_id) - event_before
                    minimum_available = min(minimum_available, shadow_available)
                start = funds()
                try:
                    if any(dep in keys and dep not in results for dep in n.depends_on):
                        raise BankError("DEPENDENCY_NOT_READY", "前置步骤尚无可核验结果")
                    raw = resolve(deepcopy(n.params), results)
                    params = self.bank.prepare(session, user_id, n.operation, raw)
                    tier = risk_tier(session, user_id, n.operation, params)
                    with session.begin_nested():
                        data = self.bank.dispatch(session, user_id, n.operation, params, "simulation_" + n.local_id)
                    results[n.local_id] = deepcopy(data)
                    after = funds()
                    amount = start["balance_fen"] - after["balance_fen"]
                    reserve = after["reserved_fen"] - start["reserved_fen"]
                    shadow_available += after["available_fen"] - start["available_fen"]
                    minimum_available = min(minimum_available, shadow_available)
                    steps.append({"node_id": n.id, "local_id": n.local_id, "operation": n.operation,
                        "tier": tier, "params": deepcopy(raw), "outflow_fen": max(0, amount),
                        "inflow_fen": max(0, -amount), "reserve_change_fen": reserve, "future": future,
                        "after": after, "reference_bound": bool(list(references(n.params))),
                        "scheduled_on": dates[n.local_id].date().isoformat()})
                    if not has_future:
                        immediate_after = after
                    if n.operation == "wealth.redeem" and data.get("status") == "PENDING":
                        deferred.append({"node_id": n.id, "amount_fen": data["amount_fen"],
                            "settles_on": data["settles_on"], "counted_as_available": False})
                except BankError as error:
                    issue = {"node_id": n.id, **error_details(error), "future": future}
                    blockers.append(issue)
                    # Funds deficit is derived from current shadow available plus this exact request.
                    wanted = n.params.get("amount_fen", 0)
                    if n.operation in {"funds.transfer", "funds.reserve", "wealth.purchase"} and isinstance(wanted, int):
                        shadow_available -= wanted
                        minimum_available = min(minimum_available, shadow_available)
                    if error.code in {"INSUFFICIENT_AVAILABLE", "PURCHASE_CONSTRAINT"} and isinstance(wanted, int):
                        issue["shortfall_fen"] = max(0, wanted - start["available_fen"])
                    steps.append({"node_id": n.id, "local_id": n.local_id, "operation": n.operation,
                        "tier": "UNKNOWN", "params": deepcopy(n.params), "outflow_fen": 0, "inflow_fen": 0,
                        "reserve_change_fen": 0, "future": future, "after": start,
                        "reference_bound": bool(list(references(n.params))), "blocked": True,
                        "scheduled_on": dates[n.local_id].date().isoformat()})
            projected = funds()
            holds = [{"purpose": d.data["purpose"], "amount_fen": d.data["remaining_fen"]}
                     for d in documents(session, "hold", user_id) if d.data["status"] == "ACTIVE"]
        finally:
            savepoint.rollback()
        # Expired ORM objects are reread from the real transaction after rollback.
        fingerprint = hashlib.sha256(canonical({"source": source, "nodes": baseline}).encode()).hexdigest()
        immediate_blockers = [b for b in blockers if not b["future"] and b["code"] != "DEPENDENCY_NOT_READY"]
        return {"fingerprint": fingerprint, "before": before, "after": projected, "immediate_after": immediate_after,
                "required_fen": max(0, before["available_fen"] - minimum_available),
                "outflow_fen": sum(s["outflow_fen"] for s in steps),
                "new_reserved_fen": sum(max(0, s["reserve_change_fen"]) for s in steps),
                "shortfall_fen": max(0, -minimum_available) if immediate_blockers else 0,
                "ready": not immediate_blockers, "complete_projection": not blockers,
                "blockers": blockers, "steps": steps, "holds": holds, "deferred_inflows": deferred,
                "known_future_debits_fen": obligations["known_future_debits_fen"],
                "simulated_recurring_debits_fen": recurring_debits, "event_warnings": event_warnings,
                "after_known_obligations_available_fen": projected["available_fen"] - max(0, obligations["known_future_debits_fen"] - recurring_debits),
                "expected_income_counted_as_available": False, "business_time": clock.isoformat(),
                "pressure": cashflow_pressure(before, steps, obligations, clock, not blockers),
                "note": "按当前规则与报价模拟剩余计划，不会记账。预约部分到期重新核验；预计收入和待到账赎回不算可用资金。"}

    def _dated_events(self, session, user_id, day, warnings):
        from .events import EventService
        charged = 0
        for redemption in documents(session, "redemption", user_id):
            if redemption.data["status"] == "PENDING" and redemption.data["settles_on"] <= day.isoformat():
                self.bank.settle_redemption(session, user_id, redemption)
        for contract in documents(session, "subscription", user_id):
            value = dict(contract.data)
            due = datetime.fromisoformat(value["next_debit"]).date()
            while due <= day:
                key = f"debit:{contract.id}:{due}"
                active = value["bank_mandate"] == value["merchant_contract"] == "ACTIVE"
                if active and not session.get(Document, key):
                    if not session.get(User, user_id).locked and available(session, user_id) >= value["amount_fen"]:
                        self.bank._transaction(session, user_id, key, value["amount_fen"], primary_account(session, user_id).id,
                            "sys_merchants", "PAYMENT", value["name"], "subscription", value["merchant_id"])
                        charged += value["amount_fen"]
                    else:
                        warnings.append(f"{due}的已登记代扣无法用当前计划剩余可用资金支付。")
                    save_document(session, key, "debit_event", user_id, {"simulation": True})
                due = EventService._next_month(due)
            contract.data = {**value, "next_debit": due.isoformat()}
        return charged

    def ensure(self, user_id, specs, review):
        preview = self.for_specs(user_id, specs, review)
        if not preview["ready"]:
            b = next(b for b in preview["blockers"] if not b["future"] and b["code"] != "DEPENDENCY_NOT_READY")
            raise BankError(b["code"], b["message"] + "。" + b["next_step"])
        return preview
