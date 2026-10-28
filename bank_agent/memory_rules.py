"""Deterministic conflict keys. Similarity never grants overwrite permission."""

import hashlib
import re

from sqlalchemy import select

from .errors import BankError
from .storage import Document, MemoryRecord, canonical, now_iso


META = "_memory_resolution"
POLICY = {
    "bank_facts": "current_verified_state_over_historical_context",
    "user_intent": "current_explicit_request_over_prior_draft_or_preference",
    "authorization": "history_never_authorizes_execution",
    "keyed_preferences": "exact_key_scope_roles_and_expected_version",
    "unkeyed_text": "append_only_no_semantic_overwrite",
    "receipts": "immutable_original_evidence",
}


def state(record):
    value = record.payload.get(META, {}) if isinstance(record.payload, dict) else {}
    return value if isinstance(value, dict) else {}


def active(record):
    return state(record).get("status", "active") == "active"


def select_active(records):
    selected, products = [], {}
    for record in records:
        if not active(record):
            continue
        if record.source_type != "product_snapshot":
            selected.append(record)
            continue
        key = (record.owner_id, record.scope, record.conversation_id, tuple(sorted(record.roles)), record.source_id)
        previous = products.get(key)
        if previous is None or (bool(state(record)), record.version, record.created_at, record.id) > (
                bool(state(previous)), previous.version, previous.created_at, previous.id):
            products[key] = record
    allowed = {r.id for r in [*selected, *products.values()]}
    return [r for r in records if r.id in allowed]


def resolve_write(session, user_id, text, payload, roles, scope, conversation_id, source_type, source_id,
                  key, expected_version, record_id):
    if META in payload:
        raise BankError("MEMORY_CONTENT_REJECTED", "记忆版本信息由服务端维护")
    if key is None and source_type == "product_snapshot":
        key = "product:" + source_id
    if key is None:
        if expected_version is not None:
            raise BankError("MEMORY_KEY_REQUIRED", "版本更新需要明确记忆键")
        return payload, 1, None
    if source_type not in {"user_preference", "product_snapshot"} or not re.fullmatch(r"[A-Za-z][\w.:-]{0,119}", key, re.ASCII):
        raise BankError("MEMORY_KEY_INVALID", "该记忆类型不能按键覆盖")
    binding = hashlib.sha256(canonical([user_id, scope, conversation_id, roles, key]).encode()).hexdigest()
    # SQL's write lock serializes revision checks across threads and workers.
    candidates = list(session.scalars(select(MemoryRecord).where(MemoryRecord.owner_id == user_id)
                                      .order_by(MemoryRecord.created_at, MemoryRecord.id)))
    matches = [r for r in candidates if state(r).get("binding") == binding]
    if source_type == "product_snapshot":
        # Attach pre-migration product snapshots by exact identity, never embedding similarity.
        matches.extend(r for r in candidates if not state(r) and r.source_type == source_type
                       and r.source_id == source_id and r.scope == scope and r.conversation_id == conversation_id
                       and sorted(r.roles) == roles)
    current = next((r for r in sorted(matches, key=lambda r: (r.version, r.created_at, r.id), reverse=True)
                    if active(r) and r.expires_at > now_iso()), None)
    version = current.version if current else 0
    head_id = "memory_head_" + binding
    head = session.get(Document, head_id)
    high_version = max([r.version for r in matches] + [head.data["version"] if head else 0])

    def save_head(memory_id, revision):
        if head:
            head.data = {"version": revision, "active_id": memory_id}
        else:
            session.add(Document(id=head_id, owner_id=user_id, kind="memory_head",
                                 data={"version": revision, "active_id": memory_id}))
    def clean(value):
        return {k: v for k, v in value.items() if k != META}
    if current and current.text == text and clean(current.payload) == payload:
        save_head(current.id, max(high_version, current.version))
        return payload, version, current.id
    if source_type == "user_preference" and expected_version != version:
        if current or expected_version is not None:
            error = BankError("MEMORY_VERSION_CONFLICT", "记忆版本已变化，请读取当前版本后再修改", 409)
            error.current_version = version
            raise error
    if current and source_type == "product_snapshot":
        previous_version, incoming_version = current.payload.get("version"), payload.get("version")
        if type(previous_version) is int and type(incoming_version) is int:
            if incoming_version < previous_version:
                raise BankError("MEMORY_SOURCE_VERSION_STALE", "旧版本产品资料不能覆盖更新版本", 409)
            if incoming_version == previous_version and (current.text != text or clean(current.payload) != payload):
                raise BankError("MEMORY_SOURCE_VERSION_CONFLICT", "同一产品版本的资料不一致，需要核对原始来源", 409)
    for previous in matches:
        if active(previous):
            previous.payload = {**previous.payload, META: {"key": key, "binding": binding, "status": "superseded",
                                                         "superseded_by": record_id}}
    metadata = {"key": key, "binding": binding, "status": "active", "superseded_by": ""}
    save_head(record_id, high_version + 1)
    return {**payload, META: metadata}, high_version + 1, None
