"""Local, owner-scoped runtime events and explicitly denominated metrics."""

from collections import Counter, defaultdict
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timedelta, timezone
import math
import re
from time import perf_counter

from sqlalchemy import select

from .runtime_context import task_state
from .storage import AgentRun, RuntimeEvent, now_iso, uid


NUMBERS = {"duration_ms", "prompt_tokens", "completion_tokens", "total_tokens", "estimated_before",
           "estimated_input_tokens", "output_reserved", "safety_margin", "context_limit", "removed_items",
           "removed_turns", "status_code", "candidate_count", "hit_count", "reread_count", "version",
           "expected_version", "attempt", "provider_status", "task_limit", "task_allocated", "reserved_tokens", "charged_tokens",
           "context_bytes_before", "context_bytes_after", "shared_object_count", "reference_count",
           'scanned_count', 'archived_count', 'duplicate_count', 'expired_count', 'quarantined_count', 'index_pruned_count'}
LABELS = {"status", "error_code", "runtime", "counter", "role", "method", "operation", "tool", "audit_kind",
          "index_backend", "source_type", "outcome", "schema_name", "memory_id", "plan_id", "research_id",
          "from", "to", "path", "stage", "role_return_id"}


def safe_metadata(values):
    result = {}
    for key, value in (values or {}).items():
        if key in NUMBERS and type(value) in {int, float} and math.isfinite(value) and value >= 0:
            result[key] = value
        elif key in LABELS and isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,160}", value, re.ASCII):
            result[key] = value
        elif key == "route" and isinstance(value, str) and re.fullmatch(r"[/\w{}:-]{1,160}", value, re.ASCII):
            result[key] = value
    return result


def append_event(session, kind, owner_id, *, run_id="", role="", task_id="", details=None):
    context = task_state.get()
    row = RuntimeEvent(
        event_id=uid("event"), owner_id=owner_id, request_id=context.request_id,
        task_id=context.task_id or task_id, conversation_id=context.conversation_id,
        run_id=run_id, role=role, kind=kind, details=safe_metadata(details), created_at=now_iso(),
    )
    session.add(row)
    return row


class RuntimeTelemetry:
    def __init__(self, db=None):
        self.db = db

    def emit(self, kind, owner_id=None, **kwargs):
        if self.db is None:
            return
        with self.db.write() as session:
            append_event(session, kind, task_state.get().owner_id if owner_id is None else owner_id, **kwargs)

    @asynccontextmanager
    async def span(self, operation, owner_id=None):
        started, outcome = perf_counter(), "failed"
        try:
            creation = asyncio.create_task(asyncio.to_thread(
                self.emit, "workflow.started", owner_id, details={"operation": operation},
            ))
            try:
                await asyncio.shield(creation)
            except asyncio.CancelledError:
                await creation
                raise
            yield
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            completion = asyncio.create_task(asyncio.to_thread(
                self.emit, "workflow.finished", owner_id,
                details={"operation": operation, "outcome": outcome, "duration_ms": (perf_counter() - started) * 1000},
            ))
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                await completion
                raise

    def events(self, owner_id, after=0, limit=100, task_id=None):
        query = select(RuntimeEvent).where(RuntimeEvent.owner_id == owner_id, RuntimeEvent.seq > after)
        if task_id:
            query = query.where(RuntimeEvent.task_id == task_id)
        with self.db.read() as session:
            rows = list(session.scalars(query.order_by(RuntimeEvent.seq).limit(limit)))
            items = [{c.name: getattr(row, c.name) for c in RuntimeEvent.__table__.columns if c.name != "owner_id"}
                     for row in rows]
        return {"items": items, "next_cursor": rows[-1].seq if rows else after}

    @staticmethod
    def _latency(values):
        values = sorted(values)
        return {"count": len(values), "avg_ms": round(sum(values) / len(values), 3) if values else None,
                "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3) if values else None}

    def metrics(self, owner_id, hours=24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.db.read() as session:
            runs = list(session.scalars(select(AgentRun).where(
                AgentRun.owner_id == owner_id, AgentRun.created_at >= since,
            )))
            events = list(session.scalars(select(RuntimeEvent).where(
                RuntimeEvent.owner_id == owner_id, RuntimeEvent.created_at >= since,
            )))
        finished = [r for r in runs if r.finished_at is not None]
        known = [r for r in runs if r.total_tokens is not None]
        groups = defaultdict(list)
        for row in finished:
            groups[row.role].append(row.duration_ms)
        requests = [e for e in events if e.kind == "request.finished"]
        budgets = [e for e in events if e.kind == "budget.checked"]
        requested = defaultdict(set)
        for event in events:
            if event.kind == "model.requested" and event.request_id:
                requested[event.request_id].add(event.run_id)
        by_id, request_tokens = {row.id: row for row in runs}, []
        for event in requests:
            associated = [by_id.get(key) for key in requested[event.request_id]]
            request_tokens.append(sum(row.total_tokens for row in associated)
                                  if all(row is not None and row.total_tokens is not None for row in associated) else None)
        known_request_tokens = [value for value in request_tokens if value is not None]
        routes = defaultdict(list)
        for event in requests:
            routes[event.details.get('route','unknown')].append(event.details['duration_ms'])
        finished_tasks = [e for e in events if e.kind=='task.finished']
        task_tokens = []
        for event in finished_tasks:
            associated = [r for r in runs if r.task_id==event.task_id]
            task_tokens.append(sum(r.total_tokens for r in associated) if all(r.total_tokens is not None for r in associated) else None)
        known_task_tokens = [v for v in task_tokens if v is not None]
        return {
            "since": since, "hours": hours,
            "model_calls": {"started": len(runs), "finished": len(finished),
                            "provider_requests": sum(e.kind == "model.requested" for e in events),
                            "statuses": dict(Counter(r.status for r in runs)),
                            "latency": self._latency([r.duration_ms for r in finished]),
                            "by_role": {role: self._latency(values) for role, values in groups.items()},
                            "usage_known_calls": len(known), "usage_unknown_calls": len(runs) - len(known),
                            "reported_total_tokens": sum(r.total_tokens for r in known) if known else None,
                            "avg_tokens_per_known_call": round(sum(r.total_tokens for r in known) / len(known), 3)
                            if known else None,
                            "reported_prompt_tokens": sum(r.prompt_tokens for r in runs if r.prompt_tokens is not None)
                            if any(r.prompt_tokens is not None for r in runs) else None,
                            "reported_completion_tokens": sum(r.completion_tokens for r in runs if r.completion_tokens is not None)
                            if any(r.completion_tokens is not None for r in runs) else None},
            "http_requests": {**self._latency([e.details["duration_ms"] for e in requests]),
                              "outcomes": dict(Counter(e.details.get("outcome") for e in requests)),
                              "tokens_known_requests": len(known_request_tokens),
                              "tokens_unknown_requests": len(request_tokens) - len(known_request_tokens),
                              "avg_attributed_tokens_per_known_request": round(sum(known_request_tokens) / len(known_request_tokens), 3)
                              if known_request_tokens else None},
            "events": dict(Counter(e.kind for e in events)),
            "budget": {"checked_calls": len(budgets),
                       "trimmed_calls": sum(e.kind == "budget.trimmed" for e in events),
                       "rejected_calls": sum(e.kind == "budget.rejected" for e in events),
                       "estimated_input_tokens": sum(e.details.get("estimated_input_tokens", 0) for e in budgets),
                       "counter": "utf8_conservative_estimate"},
            "memory": {"searches": sum(e.kind == "memory.searched" for e in events),
                       "hits": sum(e.details.get("hit_count", 0) for e in events if e.kind == "memory.searched"),
                       "rereads": sum(e.kind == "memory.reread" for e in events),
                       "write_conflicts": sum(e.kind == "memory.write.rejected" and
                                               e.details.get("error_code") == "MEMORY_VERSION_CONFLICT" for e in events)},
            "workflows": {**self._latency([e.details["duration_ms"] for e in events if e.kind == "workflow.finished"]),
                          "outcomes": dict(Counter(e.details.get("outcome") for e in events if e.kind == "workflow.finished"))},
            'chat_tasks': {'first_usable':self._latency([e.details['duration_ms'] for e in events if e.kind=='task.first_usable']),
                'total':self._latency([e.details['duration_ms'] for e in events if e.kind=='task.finished']),
                'outcomes':dict(Counter(e.details.get('status') for e in events if e.kind=='task.finished')),
                'tokens_known_tasks':len(known_task_tokens),'tokens_unknown_tasks':len(task_tokens)-len(known_task_tokens),
                'avg_tokens_per_known_task':round(sum(known_task_tokens)/len(known_task_tokens),3) if known_task_tokens else None},
            'http_by_route':{route:self._latency(values) for route,values in routes.items()},
            "errors": dict(Counter(r.error_code for r in runs if r.error_code)),
        }
