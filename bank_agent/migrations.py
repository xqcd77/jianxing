"""Small, evidence-based migration for the old all-failures security counter."""
from sqlalchemy import select

from .storage import AuditEvent, Document, User, Receipt, Transaction, LedgerEntry, audit, save_document, canonical
import hashlib

NORMAL_LEGACY_CODES = {"INSUFFICIENT_AVAILABLE", "PURCHASE_CONSTRAINT", "PRODUCT_LOCKED", "RISK_MISMATCH",
                       "ASSESSMENT_EXPIRED", "INSUFFICIENT_HOLDING", "LIMIT_DECLINED", "CARD_STATE_CONFLICT"}


def migrate_goal_state(db):
    """Additive migration: preserve goal IDs, plans, grants and original receipts."""
    with db.write() as session:
        if session.get(Document,'migration_request_runtime_v1'):
            return
        count = 0
        for doc in session.scalars(select(Document).where(Document.kind.in_(['goal_agenda','request_goals']))):
            value = dict(doc.data)
            value.setdefault('revision',1)
            value['goals'] = [{**g,'revision':g.get('revision',1)} for g in value.get('goals',[])]
            doc.data = value
            count += 1
        save_document(session,'migration_request_runtime_v1','meta','system',{'version':1,'agendas_migrated':count})
        audit(session,'system','REQUEST_RUNTIME_MIGRATED',{'version':1,'count':count})


def migrate_support_boundaries(db):
    from .storage import Node, Plan
    from .support import ensure_case
    with db.write() as session:
        if session.get(Document, "migration_support_boundaries_v2"):
            return
        for plan in session.scalars(select(Plan)):
            rows = list(session.scalars(select(Node).where(Node.plan_id == plan.id)))
            for node in rows:
                code = (node.error or {}).get("code")
                if node.status == "WAIT_HUMAN" and code in {"SECURITY_LOCKED", "OUT_OF_STOCK"}:
                    node.status = "WAIT_INPUT" if code == "SECURITY_LOCKED" else "FAILED"
                    if code == "SECURITY_LOCKED":
                        node.error = {**node.error, "category": "security", "next_step": "在安全中心验证解锁，再回到原业务核对，无需转人工。"}
                    audit(session, plan.user_id, "SELF_SERVICE_WAIT_MIGRATED", {"node_id": node.id, "code": code}, plan.id)
                if code == "CHANNEL_CIRCUIT_OPEN":
                    ensure_case(session, plan.user_id, plan, node, code)
                if code == "MERCHANT_UNSUPPORTED" and node.operation == "subscriptions.cancel":
                    from .engine import PlanService
                    from .errors import BankError
                    try:
                        params = PlanService._resolve(node.params, {n.local_id: n for n in rows})
                        ensure_case(session, plan.user_id, plan, node, code, params)
                    except BankError:
                        ensure_case(session, plan.user_id, plan, node, "EVIDENCE_UNAVAILABLE")
                partial = (node.result or {}).get("data", {})
                if partial.get("requires_human") and node.operation == "subscriptions.cancel":
                    ensure_case(session, plan.user_id, plan, node, "MERCHANT_CANCELLATION_REQUIRED",
                        {"subscription_id": partial.get("id"), "scope": node.params.get("scope", "both")})
            if plan.status == "WAIT_HUMAN" and not any(n.status == "WAIT_HUMAN" for n in rows):
                states = {n.status for n in rows}
                plan.status = next((s for s in ["UNKNOWN", "WAIT_MFA", "WAIT_CONFIRM", "WAIT_INPUT", "FAILED", "WAIT_TIME"] if s in states), "PARTIAL")
            if plan.review.get("human_handoff") and plan.status in {"WAIT_HUMAN", "UNKNOWN"}:
                ticket = session.get(Document, plan.review["human_handoff"].get("id", ""))
                if not ticket or ticket.kind != "support_case":
                    ensure_case(session, plan.user_id, plan)
        save_document(session, "migration_support_boundaries_v2", "meta", "system", {"version": 2})


def migrate_failure_policy(db):
    with db.write() as session:
        if session.get(Document, "migration_failure_policy_v2"):
            return
        for user in session.scalars(select(User)):
            circuit = session.get(Document, f"circuit_{user.id}")
            suspicion = session.get(Document, f"suspicion_{user.id}")
            if (not circuit or circuit.data.get("open") is not None or circuit.data.get("failures", 0) < 3
                or user.failed_verifications >= 3 or (suspicion and suspicion.data.get("attempts", 0) >= 3)):
                continue
            last = session.scalar(select(AuditEvent).where(AuditEvent.user_id == user.id,
                AuditEvent.kind == "CIRCUIT_OPEN").order_by(AuditEvent.seq.desc()).limit(1))
            if not last:
                continue
            blocked = list(session.scalars(select(AuditEvent).where(AuditEvent.user_id == user.id,
                AuditEvent.kind == "OPERATION_BLOCKED", AuditEvent.seq <= last.seq).order_by(AuditEvent.seq.desc()).limit(3)))
            if len(blocked) != 3 or any(e.details.get("error", {}).get("code") not in NORMAL_LEGACY_CODES for e in blocked):
                continue
            original = circuit.data["failures"]
            circuit.data = {"failures": 0, "open": False, "legacy_business_failures": original}
            unlocked = user.locked
            user.locked = False
            audit(session, user.id, "LEGACY_BUSINESS_CIRCUIT_CORRECTED", {"original_failures": original,
                  "unlocked": unlocked, "proof_event_seqs": [e.seq for e in blocked]})
        save_document(session, "migration_failure_policy_v2", "migration", "system", {"version": 2})


def migrate_business_evidence(db):
    from .services.wealth import aggregate_holding, holding_lots
    with db.write() as session:
        if session.get(Document, "migration_business_evidence_v3"):
            return
        holding_count = event_count = 0
        for doc in session.scalars(select(Document).where(Document.kind == "holding")):
            if "lots" not in doc.data:
                doc.data = aggregate_holding(doc.data["product_id"], holding_lots(doc.data), doc.data["available_after"])
                holding_count += 1
        for doc in session.scalars(select(Document).where(Document.kind.in_(["debit_event", "redemption"]))):
            value = doc.data
            if doc.kind == "debit_event" and value.get("status") == "CHARGED":
                event_id, operation = doc.id, "event.subscription"
                params = {k: value[k] for k in ("subscription_id", "due", "amount_fen")}
            elif doc.kind == "redemption" and value.get("status") == "SETTLED":
                event_id, operation = "settlement:" + doc.id, "event.redemption"
                params = {"redemption_id": doc.id, "amount_fen": value["amount_fen"]}
            else:
                continue
            if session.get(Receipt, event_id):
                continue
            txs = list(session.scalars(select(Transaction).where(Transaction.receipt_id == event_id, Transaction.owner_id == doc.owner_id)))
            entries = list(session.scalars(select(LedgerEntry).where(LedgerEntry.receipt_id == event_id)))
            if (len(txs) != 1 or len(entries) != 2 or sum(e.delta_fen for e in entries)
                    or txs[0].amount_fen != params["amount_fen"]
                    or {e.account_id: e.delta_fen for e in entries} != {txs[0].sender: -params["amount_fen"], txs[0].recipient: params["amount_fen"]}):
                audit(session, doc.owner_id, "LEGACY_EVENT_EVIDENCE_INCOMPLETE", {"event_id": event_id})
                continue
            result = {"receipt_id": event_id, "execution_mode": "sandbox", "operation": operation,
                "verified_by": "bank_backend_legacy_transaction_and_ledger", "business_time": txs[0].posted_at,
                "data": {**params, "transaction_id": txs[0].id, "balance_fen": None,
                         "historical_balance_unknown": True, "reconstructed_from_existing_evidence": True}}
            session.add(Receipt(id=event_id, owner_id=doc.owner_id, operation=operation, idempotency_key="event:" + event_id,
                params_hash=hashlib.sha256(canonical(params).encode()).hexdigest(), result=result, created_at=txs[0].posted_at))
            doc.data = {**value, "receipt_id" if doc.kind == "debit_event" else "settlement_receipt_id": event_id}
            event_count += 1
        save_document(session, "migration_business_evidence_v3", "migration", "system", {"version": 3,
            "legacy_holdings": holding_count, "reconstructed_event_receipts": event_count})
