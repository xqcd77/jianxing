from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from collections import deque
from time import perf_counter
from threading import Lock
import math

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_encrypted: Mapped[str] = mapped_column(Text)
    last_totp_counter: Mapped[int] = mapped_column(Integer, default=-1)
    failed_verifications: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_level: Mapped[int] = mapped_column(Integer, default=2)
    risk_valid_until: Mapped[str] = mapped_column(String, default="2027-10-13")
    is_demo_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[str] = mapped_column(String)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    balance_fen: Mapped[int] = mapped_column(Integer, default=0)
    system: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict] = mapped_column(JSON)


class MemoryRecord(Base):
    __tablename__ = "shared_memories"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True, default="")
    scope: Mapped[str] = mapped_column(String, default="conversation")
    kind: Mapped[str] = mapped_column(String, index=True)
    roles: Mapped[list] = mapped_column(JSON)
    source_id: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    vector: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String, default="")


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    amount_fen: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str] = mapped_column(String)
    recipient: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)
    merchant_id: Mapped[str] = mapped_column(String, default="")
    posted_at: Mapped[str] = mapped_column(String, index=True)
    receipt_id: Mapped[str] = mapped_column(String, index=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(String, index=True)
    delta_fen: Mapped[int] = mapped_column(Integer)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    operation: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    params_hash: Mapped[str] = mapped_column(String)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String)
    thread_id: Mapped[str] = mapped_column(String, unique=True)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String, default="chat")
    created_at: Mapped[str] = mapped_column(String)
    review: Mapped[dict] = mapped_column(JSON, default=dict)


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String, index=True)
    local_id: Mapped[str] = mapped_column(String)
    agent: Mapped[str] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String)
    params: Mapped[dict] = mapped_column(JSON)
    depends_on: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="READY")
    tier: Mapped[str] = mapped_column(String, default="GREEN")
    scheduled_for: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String, default="")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")


class Grant(Base):
    __tablename__ = "grants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String)
    node_id: Mapped[str] = mapped_column(String, index=True)
    fingerprint: Mapped[str] = mapped_column(String)
    tier: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    plan_id: Mapped[str] = mapped_column(String, default="")
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String)
    previous_hash: Mapped[str] = mapped_column(String)
    event_hash: Mapped[str] = mapped_column(String)


class AgentRun(Base):
    """Local, metadata-only runtime accounting; never store prompts or reasoning."""

    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    schema_name: Mapped[str] = mapped_column(String)
    runtime: Mapped[str] = mapped_column(String)
    model_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    request_id: Mapped[str] = mapped_column(String, index=True, default="")
    task_id: Mapped[str] = mapped_column(String, index=True, default="")
    conversation_id: Mapped[str] = mapped_column(String, default="")
    run_id: Mapped[str] = mapped_column(String, index=True, default="")
    role: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, index=True)


class ChatTask(Base):
    """Durable request lifecycle; independent from bank authorization and receipts."""
    __tablename__ = 'chat_tasks'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    request_key: Mapped[str] = mapped_column(String, unique=True)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, index=True, default='QUEUED')
    stage: Mapped[str] = mapped_column(String, default='queued')
    label: Mapped[str] = mapped_column(String, default='已接收需求，等待处理')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String, default='')
    lease_until: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    first_usable_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    partial: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    events: Mapped[list] = mapped_column(JSON, default=list)


class Database:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "bank.sqlite3"
        self._writer_waits = deque(maxlen=2000)
        self._metrics_lock = Lock()
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def configure(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")

        from .locking import ProcessLocks
        with ProcessLocks(data_dir).hold("bootstrap:database"):
            Base.metadata.create_all(self.engine)

    @contextmanager
    def read(self):
        with Session(self.engine, expire_on_commit=False) as session:
            yield session

    @contextmanager
    def simulation(self):
        # Preflight temporarily uses the same mutation rules as execution, but
        # can NEVER commit. Reserve the writer before the first read: upgrading
        # an old WAL read snapshot after telemetry commits causes SQLITE_BUSY.
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                with Session(bind=conn, expire_on_commit=False) as session:
                    yield session
            finally:
                conn.rollback()

    @contextmanager
    def write(self):
        # SQLite acquires its write lock before reading balances/limits. This also
        # serializes independent processes, not just threads in this API server.
        with self.engine.connect() as conn:
            started = perf_counter()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            with self._metrics_lock:
                self._writer_waits.append((perf_counter()-started)*1000)
            try:
                with Session(bind=conn, expire_on_commit=False) as session:
                    yield session
                    session.flush()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def diagnostics(self):
        with self._metrics_lock:
            waits = sorted(self._writer_waits)
        return {'backend':'sqlite_wal','coordination_scope':'same_host_shared_data_directory',
            'writer_wait':{'sample_count':len(waits),'avg_ms':round(sum(waits)/len(waits),3) if waits else None,
                          'p95_ms':round(waits[math.ceil(len(waits)*.95)-1],3) if waits else None}}


def documents(session, kind, owner=None):
    query = select(Document).where(Document.kind == kind)
    if owner is not None:
        query = query.where(Document.owner_id == owner)
    return list(session.scalars(query))


def save_document(session, doc_id, kind, owner, data):
    doc = session.get(Document, doc_id)
    if doc is None:
        doc = Document(id=doc_id, kind=kind, owner_id=owner, data=data)
        session.add(doc)
    else:
        doc.data = dict(data)
    session.flush()
    return doc


def audit(session, user_id, kind, details, plan_id=""):
    # Only metadata is admitted; credentials never enter checkpoints or audit.
    forbidden = {"password", "otp", "api_key", "secret", "access_token", "totp_secret"}

    def scrub(value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in forbidden else scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    details = scrub(details)
    previous = session.scalar(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1))
    previous_hash = previous.event_hash if previous else "0" * 64
    created = now_iso()
    payload = {"user_id": user_id, "kind": kind, "plan_id": plan_id, "details": details, "created_at": created}
    digest = hashlib.sha256((previous_hash + canonical(payload)).encode()).hexdigest()
    session.add(
        AuditEvent(
            user_id=user_id,
            kind=kind,
            plan_id=plan_id,
            details=details,
            created_at=created,
            previous_hash=previous_hash,
            event_hash=digest,
        )
    )
    # Mirror only allowlisted metadata into the runtime stream in this SAME
    # transaction. A rolled-back bank action must not publish a committed event.
    from .runtime import append_event
    event_kind = {"MEMORY_WRITTEN": "memory.written", "MEMORY_FORGOTTEN": "memory.forgotten",
                  "RESEARCH_READ": "tool.read.completed", "MODEL_REQUEST_RETRY": "model.retry",
                  "SUPERVISOR_ROUTED": "agent.routed", "WORKFLOW_TRANSITION": "workflow.transition"}.get(kind, "business.audit")
    metadata = {**details, "audit_kind": kind}
    if kind in {"MEMORY_WRITTEN", "MEMORY_FORGOTTEN"}:
        metadata["memory_id"] = details.get("id")
    if plan_id:
        metadata["plan_id"] = plan_id
    append_event(session, event_kind, user_id, task_id=plan_id,
                 role=details.get("role", ""), details=metadata)
    session.flush()
