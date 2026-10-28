"""Permission-scoped evidence memory; neither summaries nor vectors grant authority."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from threading import Lock
from time import perf_counter
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from sqlalchemy import select, or_, and_, func

from .errors import BankError
from .schemas import ProtectedContext, ProtectedField
from .storage import Document, MemoryRecord, Receipt, audit, canonical, now_iso, uid
from .memory_rules import META, POLICY, active as active_memory, resolve_write, select_active, state as memory_state
from .runtime import RuntimeTelemetry
from .memory_policy import SENSITIVE, capture_decision, contains_credentials, read_mode, ttl

DOMAINS = {"funds", "bills", "wealth", "cards", "subscriptions", "life"}
ROLES = DOMAINS | {"planner", "reviewer"}
NEGATIVE = re.compile(r"[^。！？；\n]*(?:不要|不许|禁止|不得|不能|暂不|先不|别|不动)[^。！？；\n]*")
CRITICAL_SENTENCE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:元|万|千|%|分)|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|"
    r"不要|不许|禁止|不得|仅限|不动|别动|锁定期|到账|赎回|未执行|未成功|不保证|风险|授权"
)
IMPORTANT_KEYS = {
    "amount_fen",
    "budget_fen",
    "recipient_hint",
    "recipient_id",
    "account_id",
    "date",
    "birthday_date",
    "address",
    "product_id",
    "card_id",
    "hold_id",
    "scheduled_for",
    "goal_id",
    "quote",
    "currency",
}


def _source_payload(payload):
    return {k: v for k, v in payload.items() if k != META} if isinstance(payload, dict) else payload


def terms(text):
    result = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return result


class Embeddings:
    """Local ONNX embeddings with explicitly labelled lexical fallback."""

    def __init__(self, settings, *, allow_download=False):
        self.settings = settings
        self.allow_download = allow_download
        self.model = None
        self.lock = Lock()
        self.error = ""
        self.model_id = settings.memory_model if settings.memory_embedding == "fastembed" else "lexical-char-bigram-v1"
        self.backend = "lexical" if settings.memory_embedding == "lexical" else "fastembed"

    def encode(self, texts, *, query=False):
        with self.lock:
            if self.settings.memory_embedding not in {"lexical", "fastembed"}:
                raise BankError("MEMORY_CONFIG_INVALID", "记忆检索配置无效", 503)
            if self.backend == "fastembed":
                try:
                    if self.model is None:
                        from fastembed import TextEmbedding

                        self.model = TextEmbedding(
                            model_name=self.settings.memory_model,
                            cache_dir=str(self.settings.data_dir / "embedding_cache"),
                            threads=2,
                            local_files_only=not self.allow_download,
                        )
                    function = self.model.query_embed if query else self.model.passage_embed
                    matrix = np.asarray(list(function(texts)), dtype=np.float32)
                    self.model_id = self.settings.memory_model
                    return self._normalize(matrix)
                except Exception as error:
                    # No silent claim of semantic retrieval when the model is unavailable.
                    self.backend, self.error = "lexical_fallback", type(error).__name__
            self.model_id = "lexical-char-bigram-v1"
            matrix = np.zeros((len(texts), 384), dtype=np.float32)
            for row, text in enumerate(texts):
                for token, count in Counter(terms(text)).items():
                    digest = hashlib.sha256(token.encode()).digest()
                    matrix[row, int.from_bytes(digest[:4], "little") % 384] += count
            return self._normalize(matrix)

    @staticmethod
    def _normalize(matrix):
        if matrix.ndim != 2 or not np.isfinite(matrix).all():
            raise BankError("MEMORY_VECTOR_INVALID", "记忆向量无效", 503)
        return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)


def textrank(text, query, budget=650):
    """Extractive PageRank. Financial/negative clauses bypass sentence ranking."""
    sentences = [s.strip() for s in re.split(r"(?<=[。！？；.!?\n])", text) if s.strip()]
    if len(text) <= budget:
        return text, [], False
    # Cap graph construction, but scan the entire original for critical clauses.
    query_terms = set(terms(query))
    relevant = sorted(range(len(sentences)), key=lambda i: -len(set(terms(sentences[i])) & query_terms))[:56]
    candidates = [sentences[i] for i in sorted(set([*range(min(12, len(sentences))),
        *range(max(0, len(sentences) - 12), len(sentences)), *relevant]))]
    bags = [set(terms(s)) for s in candidates]
    size = len(candidates)
    matrix = np.zeros((size, size), dtype=np.float64)
    for i in range(size):
        for j in range(i + 1, size):
            score = len(bags[i] & bags[j]) / max(1, len(bags[i] | bags[j]))
            matrix[i, j] = matrix[j, i] = score
    sums = matrix.sum(axis=1, keepdims=True)
    transition = np.divide(matrix, sums, out=np.full_like(matrix, 1 / max(1, size)), where=sums > 0)
    ranks = np.full(size, 1 / max(1, size))
    for _ in range(40):
        updated = 0.15 / max(1, size) + 0.85 * transition.T @ ranks
        if np.linalg.norm(updated - ranks, ord=1) < 1e-7:
            break
        ranks = updated
    pinned = [s for s in sentences if CRITICAL_SENTENCE.search(s)]
    query_terms = set(terms(query))
    order = sorted(range(size), key=lambda i: -(ranks[i] + 0.2 * len(bags[i] & query_terms) / max(1, len(query_terms))))
    selected, used = set(), 0
    for i in order:
        if candidates[i] in pinned:
            continue
        if used + len(candidates[i]) <= budget:
            selected.add(i)
            used += len(candidates[i])
    summary = "".join(candidates[i] for i in sorted(selected))
    # Pinned originals are separately returned; the caller either includes all or rereads.
    return summary, list(dict.fromkeys(pinned)), True


def protected_context(message, context=None, facts=None):
    context, facts = context or {}, facts or {}
    active = context.get("active_plan", {})
    result = ProtectedContext(plan_id=active.get("id"), plan_version=active.get("version"))
    result.negative_constraints = NEGATIVE.findall(message)
    from .intent_constraints import forbidden_operations
    result.forbidden_operations = forbidden_operations(message)
    for source_type, source_id, tasks in [
        ("bank_state", active.get("id", ""), active.get("nodes", [])),
        ("draft", "pending_draft", context.get("pending_draft", {}).get("intent", {}).get("tasks", [])),
    ]:
        for task in tasks:
            for key, value in task.get("params", {}).items():
                if key in IMPORTANT_KEYS:
                    result.fields.append(
                        ProtectedField(name=key, value=value, source_id=source_id, source_type=source_type)
                    )
            if task.get("scheduled_for"):
                result.fields.append(
                    ProtectedField(
                        name="scheduled_for",
                        value=task["scheduled_for"],
                        source_id=source_id,
                        source_type=source_type,
                    )
                )
            receipt_id = (task.get("result") or {}).get("receipt_id")
            if receipt_id:
                result.receipt_refs.append(receipt_id)
    # Preserve current utterance verbatim so unsupported amounts/negations are never lost.
    result.fields.append(
        ProtectedField(name="current_request", value=message, source_id="current_message", source_type="user_message")
    )
    from .dialogue import money_slot

    try:
        amount = money_slot(message)
    except BankError:
        amount = None
    if amount is not None:
        result.fields.append(
            ProtectedField(name="amount_fen", value=amount, source_id="current_message", source_type="user_message")
        )
    for value in re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日", message):
        result.fields.append(
            ProtectedField(name="date_expression", value=value, source_id="current_message", source_type="user_message")
        )
    for contact in facts.get("contacts", []):
        if contact.get("name") and contact["name"] in message:
            result.fields.append(
                ProtectedField(
                    name="verified_recipient",
                    value=contact,
                    source_id="current_contacts",
                    source_type="bank_state",
                )
            )
    return result


def observe_write(function):
    @wraps(function)
    def wrapped(self, user_id, *args, **kwargs):
        try:
            return function(self, user_id, *args, **kwargs)
        except BankError as error:
            self.telemetry.emit("memory.write.rejected", user_id, details={
                "error_code": error.code, "version": getattr(error, "current_version", None),
            })
            raise
    return wrapped


class SharedMemory:
    def __init__(self, db, bank, settings):
        self.db, self.bank, self.settings = db, bank, settings
        if settings.memory_backend not in {"chroma", "numpy"}:
            raise BankError("MEMORY_CONFIG_INVALID", "记忆索引配置无效", 503)
        from .memory_index import ChromaMemoryIndex
        self.index = (ChromaMemoryIndex(settings.data_dir, settings.memory_chroma_url)
                      if settings.memory_backend == "chroma" else None)
        self.embeddings = Embeddings(settings)
        self.telemetry = RuntimeTelemetry(db)
        self._catalog_versions = set()
        self._catalog_lock = Lock()
        self._index_worker = ThreadPoolExecutor(max_workers=1,thread_name_prefix='bank-memory-index') if self.index else None
        self._index_embeddings = Embeddings(settings) if self.index else None
        self._index_jobs = {}
        self._jobs_lock = Lock()
        self._closed = False
        from .memory_curator import MemoryCurator
        self.curator = MemoryCurator(self)

    def queue_index(self, record_id):
        if self._index_worker is None or self._closed:
            return
        with self._jobs_lock:
            previous = self._index_jobs.get(record_id)
            if previous and not previous.done():
                return
            future = self._index_worker.submit(self._prepare_index,record_id)
            self._index_jobs[record_id] = future
        def cleanup(done):
            with self._jobs_lock:
                if self._index_jobs.get(record_id) is done:
                    self._index_jobs.pop(record_id,None)
        future.add_done_callback(cleanup)

    def _prepare_index(self, record_id):
        owner_id = ''
        try:
            with self.db.read() as session:
                row = session.get(MemoryRecord,record_id)
                if not row or row.expires_at <= now_iso() or not active_memory(row):
                    return
                owner_id = row.owner_id
            if row.source_type in {'bank_receipt','research_report'}:
                row.text = self.reread(owner_id,row.id,'planner',row.conversation_id)['text']
                row.payload = json.loads(row.text)
            if contains_credentials(row.text, _source_payload(row.payload)):
                return
            vector = self._index_embeddings.encode([row.text])[0]
            tag = self._index_embeddings.model_id+'#'+hashlib.sha256(row.text.encode()).hexdigest()
            with self.db.write() as session:
                current = session.get(MemoryRecord,record_id)
                if not current or current.version != row.version or not active_memory(current) or current.expires_at <= now_iso():
                    return
                current.vector,current.embedding_model = vector.tolist(),tag
            self.index.upsert([row],[vector],self._index_embeddings.model_id,[tag])
        except Exception:
            # SQL is authoritative. A failed warm-up is visible and a later read
            # or rebuild retries it; it cannot change a source or permission.
            self.telemetry.emit('memory.index_sync_failed',owner_id,details={'error_code':'MEMORY_INDEX_UNAVAILABLE'})

    def rebuild_index(self):
        with self.db.read() as session:
            ids = list(session.scalars(select(MemoryRecord.id).where(MemoryRecord.expires_at > now_iso())))
        for record_id in ids:
            self.queue_index(record_id)
        return {'queued':len(ids)}

    @observe_write
    def remember(
        self,
        user_id,
        text,
        *,
        kind="dialogue",
        roles=None,
        conversation_id="",
        scope="conversation",
        source_id="",
        source_type="user_message",
        payload=None,
        ttl_days=None,
        record_id=None,
        conflict_key=None,
        expected_version=None,
    ):
        if (payload is not None and not isinstance(payload, dict)) or contains_credentials(text, payload) \
                or kind not in {"dialogue", "preference", "product", "receipt", "analysis"}:
            raise BankError("MEMORY_CONTENT_REJECTED", "敏感凭证不能写入记忆")
        roles = sorted(set(DOMAINS if roles is None else roles))
        if not set(roles) <= DOMAINS or scope not in {"conversation", "user"}:
            raise BankError("MEMORY_SCOPE_INVALID", "记忆访问范围无效")
        if source_type not in {"user_message", "bank_receipt", "product_snapshot", "user_preference", "research_report"}:
            raise BankError("MEMORY_SOURCE_INVALID", "记忆来源无效")
        ttl_days = ttl(source_type, ttl_days)
        if scope == "conversation" and not conversation_id:
            raise BankError("MEMORY_SCOPE_INVALID", "会话记忆需要会话标识")
        record_id = record_id or uid("memory")
        with self.db.write() as session:
            existing = session.get(MemoryRecord, record_id)
            if existing:
                if existing.owner_id != user_id:
                    raise BankError("MEMORY_NOT_FOUND", "记忆不存在或无权访问", 404)
                if (existing.source_type != source_type or existing.source_id != (source_id or record_id)
                    or existing.scope != scope or existing.conversation_id != conversation_id or existing.roles != roles
                    or (source_type != "bank_receipt" and (existing.text != text
                        or {k: v for k, v in existing.payload.items() if k != META} != (payload or {})))):
                    raise BankError("MEMORY_IMMUTABLE", "同一记忆ID不能改写原始证据，请创建新版本", 409)
                return existing.id
            if source_type == "bank_receipt":
                receipt = session.get(Receipt, source_id)
                if not receipt or receipt.owner_id != user_id:
                    raise BankError("MEMORY_SOURCE_INVALID", "银行记忆需要本人有效回执")
                payload = receipt.result
                text = canonical(payload)
                if contains_credentials(text, payload):
                    audit(
                        session,
                        user_id,
                        "MEMORY_SOURCE_EXCLUDED",
                        {"source_id": source_id, "reason": "sensitive_content"},
                    )
                    return None
            if source_type == "research_report":
                source = session.get(Document, source_id)
                if not source or source.kind != "research" or source.owner_id != user_id or not source.data.get("report"):
                    raise BankError("MEMORY_SOURCE_INVALID", "研究报告来源无效")
            resolved_payload, version, duplicate = resolve_write(
                session, user_id, text, payload or {}, roles, scope, conversation_id, source_type, source_id,
                conflict_key, expected_version, record_id,
            )
            if duplicate:
                return duplicate
            session.add(
                MemoryRecord(
                    id=record_id,
                    owner_id=user_id,
                    conversation_id=conversation_id,
                    scope=scope,
                    kind=kind,
                    roles=roles,
                    source_id=source_id or record_id,
                    source_type=source_type,
                    text=text,
                    payload=resolved_payload,
                    version=version,
                    created_at=now_iso(),
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(),
                )
            )
            # Commit the source and its coalesced background job together. Old
            # originals are archived by policy, never deleted to meet a top-K cap.
            self.curator.enqueue(session, user_id)
            audit(
                session,
                user_id,
                "MEMORY_WRITTEN",
                {"id": record_id, "kind": kind, "roles": roles, "source_type": source_type, "version": version},
            )
        self.queue_index(record_id)
        return record_id

    def capture(self, user_id, conversation_id, message, response, intent=None):
        decision = capture_decision(message, response, intent)
        if decision != 'capture_task_context':
            self.telemetry.emit('memory.capture_skipped', user_id, details={'method': decision})
            # Even a short continuation may have produced a genuine new receipt.
            self.capture_results(user_id, conversation_id, response)
            return
        tasks = [*(intent or {}).get('tasks', []),
                 *[g['pending_task'] for g in (intent or {}).get('additional_goals', []) if g.get('pending_task')]]
        domains = sorted({str(t.get("domain")) for t in tasks})
        domains = [d for d in domains if d in DOMAINS]
        self.remember(user_id, message, roles=domains, conversation_id=conversation_id, source_id=uid("utterance"))
        self.capture_results(user_id, conversation_id, response)

    def capture_results(self, user_id, conversation_id, response):
        for node in response.get("nodes", []):
            receipt_id = (node.get("result") or {}).get("receipt_id")
            if receipt_id:
                self.remember(
                    user_id,
                    "",
                    kind="analysis" if node["agent"] == "bills" else "receipt",
                    roles=[node["agent"]],
                    conversation_id=conversation_id,
                    source_id=receipt_id,
                    source_type="bank_receipt",
                    record_id="memory_" + receipt_id,
                )

    def seed_products(self, user_id, facts):
        for product in facts.get("products", []):
            text = canonical(product)
            key = "memory_product_" + user_id + "_" + hashlib.sha256(text.encode()).hexdigest()[:20]
            with self._catalog_lock:
                if key in self._catalog_versions:
                    continue
            self.remember(
                user_id,
                text,
                kind="product",
                roles=["wealth"],
                scope="user",
                source_id=product["id"],
                source_type="product_snapshot",
                ttl_days=7,
                record_id=key,
                payload=product,
            )
            with self._catalog_lock:
                self._catalog_versions.add(key)

    @staticmethod
    def _allowed(record, user_id, role, conversation_id):
        return (
            record.owner_id == user_id
            and record.expires_at > now_iso()
            and (record.scope == "user" or record.conversation_id == conversation_id)
            and (role in {"planner", "reviewer"} or role in record.roles)
        )

    def reread(self, user_id, record_id, role="planner", conversation_id=None):
        if role not in ROLES:
            raise BankError("MEMORY_SCOPE_INVALID", "记忆角色无效")
        with self.db.read() as session:
            record = session.get(MemoryRecord, record_id)
            if not record or not self._allowed(record, user_id, role, conversation_id):
                raise BankError("MEMORY_NOT_FOUND", "记忆不存在、已失效或无权访问", 404)
            text = record.text
            if record.source_type == "bank_receipt":
                receipt = session.get(Receipt, record.source_id)
                if not receipt or receipt.owner_id != user_id:
                    raise BankError("MEMORY_SOURCE_INVALID", "原始银行回执不可用")
                text = canonical(receipt.result)
            if record.source_type == "research_report":
                source = session.get(Document, record.source_id)
                if not source or source.kind != "research" or source.owner_id != user_id:
                    raise BankError("MEMORY_SOURCE_INVALID", "原始研究报告不可用")
                text = canonical([c for c in source.data.get("report", {}).get("findings", [])
                                  if c["id"].split(":")[0] in record.roles
                                  and (role in {"planner", "reviewer"} or c["id"].startswith(role + ":"))])
            result = {
                "id": record.id,
                "text": text,
                "source_id": record.source_id,
                "source_type": record.source_type,
                "version": record.version,
                "historical_only": True,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "memory_status": memory_state(record).get("status", "active"),
                "superseded_by": memory_state(record).get("superseded_by", ""),
            }
            if contains_credentials(text):
                raise BankError('MEMORY_SOURCE_INVALID', '原始记忆包含敏感凭证，不能用于上下文', 409)
        self.telemetry.emit("memory.reread", user_id, role=role,
                            details={"memory_id": record_id, "source_type": result["source_type"]})
        return result

    def search(self, user_id, query, role="planner", conversation_id=None, protected=None, *, _preferences_only=False):
        started = perf_counter()
        if role not in ROLES or SENSITIVE.search(query):
            raise BankError("MEMORY_SCOPE_INVALID", "检索请求无效")
        with self.db.read() as session:
            conditions = [func.coalesce(func.json_extract(MemoryRecord.payload, '$.' + META + '.status'), 'active') == 'active']
            if role not in {'planner', 'reviewer'}:
                roles = func.json_each(MemoryRecord.roles).table_valued('value').alias('allowed_memory_roles')
                conditions.append(select(1).select_from(roles).where(roles.c.value == role).exists())
            if _preferences_only:
                conditions.append(MemoryRecord.kind == 'preference')
            candidates = list(
                session.scalars(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.owner_id == user_id,
                        MemoryRecord.expires_at > now_iso(),
                        *conditions,
                        or_(MemoryRecord.scope=='user',and_(MemoryRecord.scope=='conversation',
                            MemoryRecord.conversation_id==(conversation_id or ''))),
                    )
                    .order_by((MemoryRecord.kind=='preference').desc(),MemoryRecord.created_at.desc())
                    .limit(None if _preferences_only else 512)
                )
            )
            if not _preferences_only:
                # Mandatory preferences cannot be crowded out by recent messages.
                required = list(session.scalars(select(MemoryRecord).where(
                    MemoryRecord.owner_id == user_id, MemoryRecord.expires_at > now_iso(),
                    MemoryRecord.kind == 'preference', *conditions,
                    or_(MemoryRecord.scope == 'user', and_(MemoryRecord.scope == 'conversation',
                        MemoryRecord.conversation_id == (conversation_id or ''))))))
                candidates = list({r.id: r for r in [*candidates, *required]}.values())
            # ACL and conversation filters MUST precede any embedding/ranking.
            rows = select_active([r for r in candidates if self._allowed(r, user_id, role, conversation_id)])
        protected = protected or ProtectedContext()
        if self.settings.memory_strategy not in {"none", "raw", "compressed"}:
            raise BankError("MEMORY_CONFIG_INVALID", "共享记忆策略配置无效")
        if self.settings.memory_strategy == "none":
            rows = []
        hits = []
        if rows and _preferences_only:
            # Required preferences are read from authorized SQL originals; a
            # balance query does not need Chroma or an embedding call.
            with self.db.read() as session:
                fresh_rows = list(session.scalars(select(MemoryRecord).where(
                    MemoryRecord.id.in_([row.id for row in rows]), MemoryRecord.owner_id == user_id)))
            for row in fresh_rows:
                if not active_memory(row) or not self._allowed(row, user_id, role, conversation_id):
                    continue
                original = self.reread(user_id, row.id, role, conversation_id)
                row.text = original['text']
                if row.source_type in {'bank_receipt', 'research_report'}:
                    row.payload = json.loads(original['text'])
                hits.append((row, 1.0))
        if rows and not _preferences_only:
            # Backed evidence is reread before embedding, not trusted as cached text.
            for row in rows:
                if row.source_type in {"bank_receipt", "research_report"}:
                    evidence = self.reread(user_id, row.id, role, conversation_id)
                    row.text, row.payload = evidence["text"], json.loads(evidence["text"])
                if contains_credentials(row.text, _source_payload(row.payload)):
                    raise BankError('MEMORY_SOURCE_INVALID', '记忆来源含敏感凭证，不能用于上下文', 409)
            query_vector = self.embeddings.encode([query], query=True)[0]
            indexed_model = self.embeddings.model_id
            vectors = [None] * len(rows)
            missing = []
            tags = [indexed_model + "#" + hashlib.sha256(r.text.encode()).hexdigest() for r in rows]
            for i, row in enumerate(rows):
                if row.embedding_model == tags[i] and row.vector is not None:
                    vector = np.asarray(row.vector, dtype=np.float32)
                    if vector.shape == query_vector.shape and np.isfinite(vector).all():
                        vectors[i] = vector
                        continue
                missing.append(i)
            if missing:
                encoded = self.embeddings.encode([rows[i].text for i in missing])
                for i, vector in zip(missing, encoded, strict=True):
                    vectors[i] = vector
            if indexed_model != self.embeddings.model_id:
                query_vector = self.embeddings.encode([query], query=True)[0]
                vectors = list(self.embeddings.encode([r.text for r in rows]))
                missing = list(range(len(rows)))
                tags = [self.embeddings.model_id + "#" + hashlib.sha256(r.text.encode()).hexdigest() for r in rows]
            vectors = np.asarray(vectors)
            # Preserve index provenance in SQLite; no vectors are exposed through APIs.
            with self.db.write() as session:
                for i in missing:
                    row, vector = rows[i], vectors[i]
                    record = session.get(MemoryRecord, row.id)
                    if record and self._allowed(record, user_id, role, conversation_id):
                        record.vector, record.embedding_model = vector.tolist(), tags[i]
            count = max(1, min(self.settings.memory_top_k, 12))
            if self.index is not None:
                hits = self.index.rank(rows, vectors, query_vector, self.embeddings.model_id, tags,
                                       user_id, role, conversation_id, count)
            else:
                scores = vectors @ query_vector
                hits = [(rows[int(i)], float(scores[i])) for i in np.argsort(-scores)[:count]]
            # An index call can take time. Recheck live SQL access after ranking,
            # so deletion, expiry or a scope change during retrieval takes effect.
            with self.db.read() as session:
                current = {r.id: r for r in session.scalars(
                    select(MemoryRecord).where(MemoryRecord.id.in_([r.id for r in rows]))
                )}
            removed = [r.id for r in rows if r.id not in current]
            if removed and self.index is not None:
                self.index.delete(removed)
            checked = []
            protected_ids = {r.id for r in rows if r.kind == 'preference' and
                             (CRITICAL_SENTENCE.search(r.text) or NEGATIVE.search(r.text))}
            known = {r.id for r, _ in hits}
            hits = [(r, 1.0) for r in rows if r.id in protected_ids - known] + hits
            for row, score in hits:
                fresh = current.get(row.id)
                if fresh is None or not self._allowed(fresh, user_id, role, conversation_id) or not active_memory(fresh):
                    continue
                evidence = self.reread(user_id, row.id, role, conversation_id)
                fresh.text = evidence["text"]
                if fresh.source_type in {"bank_receipt", "research_report"}:
                    fresh.payload = json.loads(evidence["text"])
                checked.append((fresh, score))
            hits = checked
        for row, _ in hits:
            if row.kind == 'preference' and NEGATIVE.search(row.text) and row.text not in protected.negative_constraints:
                protected.negative_constraints.append(row.text)
        limit = max(1000, self.settings.memory_context_chars)
        items, reread_required = [], []
        original_chars, compressed_chars = 0, 0
        for row, score in hits:
            summary, originals, compressed = textrank(row.text, query, budget=min(650, limit))
            if self.settings.memory_strategy == "raw":
                summary, originals, compressed = row.text, [], False
            fields = []

            def pin(value, path=""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        location = path + "." + key if path else key
                        if key in IMPORTANT_KEYS or key.endswith("_fen") or key.endswith("_date"):
                            fields.append(
                                ProtectedField(
                                    name=location, value=item, source_id=row.source_id, source_type=row.source_type
                                )
                            )
                        else:
                            pin(item, location)
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        pin(item, f"{path}[{i}]")

            pin(row.payload)
            protected.fields.extend(fields)
            original_chars += len(row.text)
            # Financial clauses are never truncated to satisfy the soft budget.
            value = summary + "".join(originals)
            if len(value) > limit:
                reread_required.append(row.id)
                continue
            items.append(
                {
                    "id": row.id,
                    "source_id": row.source_id,
                    "source_type": row.source_type,
                    "kind": row.kind,
                    "text": value,
                    "critical_originals": originals,
                    "protected_fields": [f.model_dump(mode="json") for f in fields],
                    "score": round(score, 6),
                    "compressed": compressed,
                    "historical_only": True,
                    "version": row.version,
                }
            )
            compressed_chars += len(value)
            limit -= len(value)
        # L3 can be used explicitly by verifier/backtrack; oversized evidence is not silently discarded.
        result = {
            "protected": protected.model_dump(mode="json"),
            "items": items,
            "reread_required": reread_required,
            "embedding_backend": self.embeddings.backend,
            "index_backend": self.settings.memory_backend,
            "embedding_error": self.embeddings.error,
            "original_chars": original_chars,
            "compressed_chars": compressed_chars,
            "compression_ratio": round(compressed_chars / original_chars, 4) if original_chars else 1,
            "budget_exceeded": bool(reread_required),
            "authority": "context_only",
            "memory_strategy": self.settings.memory_strategy,
            "resolution_policy": POLICY,
            'read_policy': 'current_facts_and_preferences' if _preferences_only else 'semantic_recall',
        }
        result["protected_chars"] = len(canonical(result["protected"]))
        result["context_chars"] = len(canonical(result))
        self.telemetry.emit("memory.searched", user_id, role=role, details={
            "candidate_count": len(rows), "hit_count": len(items), "reread_count": len(reread_required),
            "index_backend": self.settings.memory_backend, "duration_ms": (perf_counter() - started) * 1000,
            'method': result['read_policy'],
        })
        return result

    def forget(self, user_id, record_id):
        with self.db.read() as session:
            record = session.get(MemoryRecord, record_id)
            if not record or record.owner_id != user_id:
                raise BankError("MEMORY_NOT_FOUND", "记忆不存在或无权访问", 404)
        if self.index is not None:
            self.index.delete([record_id])
        with self.db.write() as session:
            record = session.get(MemoryRecord, record_id)
            if not record or record.owner_id != user_id:
                raise BankError("MEMORY_NOT_FOUND", "记忆不存在或无权访问", 404)
            binding = memory_state(record).get("binding")
            if binding:
                head = session.get(Document, "memory_head_" + binding)
                if head and head.data.get("active_id") == record_id:
                    head.data = {**head.data, "active_id": ""}
            session.delete(record)
            audit(session, user_id, "MEMORY_FORGOTTEN", {"id": record_id})

    def close(self):
        self._closed = True
        if self._index_worker is not None:
            self._index_worker.shutdown(wait=True,cancel_futures=True)
        if self.index is not None:
            self.index.close()

    def search_for_request(self, user_id, query, role='planner', conversation_id=None, protected=None):
        """Optional recall can fail open only for a fully explicit, independent read/action."""
        try:
            if self.curator.enabled and read_mode(query, protected) == 'current_facts_and_preferences':
                return self.search(user_id, query, role, conversation_id, protected, _preferences_only=True)
            return self.search(user_id,query,role,conversation_id,protected)
        except BankError as error:
            if error.code != 'MEMORY_INDEX_UNAVAILABLE' or re.search(r'她|他|那个|上次|刚才|同样|剩下|其余|继续|改成|取消',query):
                raise
            from .request_understanding import understand_request
            facts = self.bank.facts(user_id)
            intent = understand_request(query,facts,{})
            if not intent or intent.clarification or intent.additional_goals or len(intent.tasks)!=1 or intent.tasks[0].action not in {'funds.transfer','balance.query'}:
                raise
            with self.db.read() as session:
                rows = list(session.scalars(select(MemoryRecord).where(MemoryRecord.owner_id==user_id)))
                domain = str(intent.tasks[0].domain)
                required = [r for r in select_active(rows) if self._allowed(r,user_id,role,conversation_id)
                    and r.source_type in {'user_message','user_preference'} and domain in r.roles and NEGATIVE.search(r.text)]
            if required:
                raise
            self.telemetry.emit('memory.degraded',user_id,role=role,details={'error_code':error.code})
            return {'protected':(protected or ProtectedContext()).model_dump(mode='json'),'items':[],
                'reread_required':[],'degraded':True,'error_code':error.code,'authority':'context_only',
                'notice':'历史资料暂不可用，本次仅按你明确提供的信息与实时账户核对。'}

    def list(self, user_id):
        with self.db.read() as session:
            records = list(
                session.scalars(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.owner_id == user_id,
                        MemoryRecord.expires_at > now_iso(),
                    )
                    .order_by(MemoryRecord.created_at.desc())
                    .limit(100)
                )
            )
            return [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "scope": r.scope,
                    "roles": r.roles,
                    "source_id": r.source_id,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "conversation_id": r.conversation_id,
                    "version": r.version,
                    "key": memory_state(r).get("key"),
                    "status": memory_state(r).get("status", "active"),
                    "superseded_by": memory_state(r).get("superseded_by", ""),
                }
                for r in records
            ]
