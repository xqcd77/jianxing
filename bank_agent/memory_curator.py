"""Durable, bounded background curation of derived memory. Never edits sources."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import func, or_, select

from .errors import BankError
from .locking import ProcessLocks
from .memory_policy import contains_credentials
from .memory_rules import META, active, state
from .storage import Document, MemoryRecord, Plan, canonical, now_iso


def _after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _key(owner):
    return 'memory_maintenance_' + hashlib.sha256(owner.encode()).hexdigest()


def source_digest(row):
    return hashlib.sha256(canonical([row.owner_id, row.scope, row.conversation_id, sorted(row.roles),
        row.source_type, row.source_id, row.kind, row.text, row.version, row.expires_at,
        {k: v for k, v in row.payload.items() if k != META}]).encode()).hexdigest()


def _identity(row):
    body = {k: v for k, v in row.payload.items() if k != META}
    identity = [row.owner_id, row.scope, row.conversation_id, sorted(row.roles), row.kind, row.source_type,
                row.text, body, state(row).get('binding'), state(row).get('key'),
                '' if row.source_type == 'user_message' else row.source_id]
    return 'memory_digest_' + hashlib.sha256(canonical(identity).encode()).hexdigest()


class MemoryCurator:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        settings = memory.settings
        self.enabled = settings.memory_maintenance_enabled and settings.orchestration_mode == 'supervisor'
        self.interval = settings.memory_maintenance_interval
        self.batch = settings.memory_maintenance_batch
        self.active_dialogues = settings.memory_active_dialogues
        if self.interval < 1 or not 1 <= self.batch <= 512 or not 1 <= self.active_dialogues <= 512:
            raise BankError('MEMORY_CONFIG_INVALID', '后台记忆整理配置无效', 503)
        self.locks = ProcessLocks(settings.data_dir)

    def enqueue(self, session, owner):
        if not self.enabled:
            return
        job = session.get(Document, _key(owner))
        if job:
            job.data = {**job.data, 'generation': job.data['generation'] + 1,
                'status': 'running' if job.data['status'] == 'running' else 'pending', 'next_run_at': now_iso()}
        else:
            session.add(Document(id=_key(owner), kind='memory_maintenance', owner_id=owner, data={
                'status': 'pending', 'generation': 1, 'processed_generation': 0, 'cursor': '',
                'next_run_at': now_iso(), 'lease_until': '', 'attempt': 0, 'pending_delete_ids': []}))

    def bootstrap(self):
        if not self.enabled:
            return
        with self.db.write() as session:
            for owner in session.scalars(select(MemoryRecord.owner_id).distinct()):
                self.enqueue(session, owner)

    @staticmethod
    def _due():
        return or_(Document.data['status'].as_string().in_(['pending', 'retry', 'idle']) &
                   (Document.data['next_run_at'].as_string() <= now_iso()),
                   (Document.data['status'].as_string() == 'running') &
                   (Document.data['lease_until'].as_string() <= now_iso()))

    def run_pending(self, max_owners=4):
        if not self.enabled:
            return {'enabled': False, 'jobs': 0, 'more': False}
        try:
            with self.locks.hold('memory:maintenance', timeout=0):
                with self.db.read() as session:
                    ids = list(session.scalars(select(Document.id).where(
                        Document.kind == 'memory_maintenance', self._due())
                        .order_by(Document.data['next_run_at'].as_string(), Document.id).limit(max_owners)))
                for job_id in ids:
                    self._run(job_id)
                with self.db.read() as session:
                    more = session.scalar(select(Document.id).where(
                        Document.kind == 'memory_maintenance', self._due()).limit(1)) is not None
                return {'enabled': True, 'jobs': len(ids), 'more': more}
        except BankError as error:
            if error.code != 'PLAN_BUSY':
                raise
            return {'enabled': True, 'jobs': 0, 'more': False, 'busy': True}

    def _run(self, job_id):
        with self.db.write() as session:
            job = session.get(Document, job_id)
            owner, data = job.owner_id, dict(job.data)
            generation = data['generation']
            job.data = {**data, 'status': 'running', 'lease_until': _after(300)}
        stats = {'scanned_count': 0, 'archived_count': 0, 'duplicate_count': 0, 'expired_count': 0,
                 'quarantined_count': 0, 'index_pruned_count': 0}
        try:
            self._prune(data.get('pending_delete_ids', []))
            with self.db.read() as session:
                ids = list(session.scalars(select(MemoryRecord.id).where(
                    MemoryRecord.owner_id == owner, MemoryRecord.id > data.get('cursor', ''))
                    .order_by(MemoryRecord.id).limit(self.batch)))
                recent = set(session.scalars(select(MemoryRecord.id).where(
                    MemoryRecord.owner_id == owner, MemoryRecord.kind == 'dialogue')
                    .order_by(MemoryRecord.created_at.desc(), MemoryRecord.id.desc()).limit(self.active_dialogues)))
                open_chats = self._open_conversations(session, owner)
            delete_ids = []
            for record_id in ids:
                outcome = self._organize(owner, record_id, recent, open_chats)
                stats['scanned_count'] += 1
                if outcome in {'archived', 'duplicate', 'expired', 'quarantined'}:
                    stats[outcome + '_count'] += 1
                if outcome != 'active':
                    delete_ids.append(record_id)
                else:
                    with self.db.read() as session:
                        live = session.get(MemoryRecord, record_id)
                        original_hash = state(live).get('curation', {}).get('original_sha256') if live else None
                        model = self.memory._index_embeddings.model_id if self.memory._index_embeddings else ''
                        needs_index = live and (live.vector is None or live.embedding_model != model + '#' + str(original_hash))
                    if needs_index:
                        self.memory.queue_index(record_id)
            # Keep retryable vector deletions durable before contacting Chroma.
            with self.db.write() as session:
                job = session.get(Document, job_id)
                job.data = {**job.data, 'pending_delete_ids': delete_ids}
            self._prune(delete_ids)
            stats['index_pruned_count'] = len(delete_ids)
            with self.db.write() as session:
                job = session.get(Document, job_id)
                changed = job.data['generation'] != generation
                complete = len(ids) < self.batch and not changed
                job.data = {**job.data, 'status': 'idle' if complete else 'pending',
                    'processed_generation': generation if complete else job.data['processed_generation'],
                    'cursor': '' if complete or changed else ids[-1], 'pending_delete_ids': [],
                    'next_run_at': _after(self.interval) if complete else now_iso(), 'lease_until': '',
                    'attempt': 0, 'error_code': '', 'last_run_at': now_iso(), 'last_batch': stats}
            self.memory.telemetry.emit('memory.curated', owner, details={**stats, 'status': 'idle' if complete else 'pending'})
        except Exception:
            with self.db.write() as session:
                job = session.get(Document, job_id)
                attempt = job.data.get('attempt', 0) + 1
                job.data = {**job.data, 'status': 'retry', 'attempt': attempt, 'lease_until': '',
                    'next_run_at': _after(min(300, 2 ** min(attempt, 8))), 'error_code': 'MEMORY_MAINTENANCE_FAILED'}
            self.memory.telemetry.emit('memory.curation_failed', owner,
                details={'error_code': 'MEMORY_MAINTENANCE_FAILED', 'attempt': attempt})

    def _prune(self, ids):
        if ids and self.memory.index is not None:
            self.memory.index.delete(ids)

    @staticmethod
    def _open_conversations(session, owner):
        terminal = {'COMPLETED', 'CANCELLED', 'SUCCEEDED', 'ANSWERED', 'UNSUPPORTED'}
        chats = set(session.scalars(select(Plan.conversation_id).where(
            Plan.user_id == owner, Plan.status.not_in(['COMPLETED', 'CANCELLED', 'FAILED']))))
        for doc in session.scalars(select(Document).where(Document.owner_id == owner,
                or_(Document.kind.in_(['draft', 'goal_agenda']),
                    (Document.kind == 'research') & (Document.data['status'].as_string() == 'RESEARCH_RUNNING')))):
            if doc.kind == 'draft':
                chats.add(doc.id.removeprefix('draft_' + owner + '_'))
            elif doc.kind == 'goal_agenda' and any(g.get('status') not in terminal for g in doc.data.get('goals', [])):
                chats.add(doc.data.get('conversation_id', ''))
            elif doc.kind == 'research' and doc.data.get('status') == 'RESEARCH_RUNNING':
                chats.add(doc.data.get('conversation_id', ''))
        return chats

    def _organize(self, owner, record_id, recent, open_chats):
        from .memory import CRITICAL_SENTENCE, textrank
        with self.db.read() as session:
            row = session.get(MemoryRecord, record_id)
            if row is None or row.owner_id != owner:
                return 'missing'
            fingerprint = source_digest(row)
            original = row.text
        current_state = state(row)
        outcome, reason = current_state.get('status', 'active'), current_state.get('reason', '')
        if row.expires_at <= now_iso():
            outcome, reason = 'expired', 'ttl_elapsed'
        elif contains_credentials(row.text, {k: v for k, v in row.payload.items() if k != META}):
            outcome, reason = 'quarantined', 'credentials_excluded'
        elif active(row) and row.kind == 'dialogue' and row.source_type == 'user_message' and row.scope == 'conversation' \
                and row.id not in recent and row.conversation_id not in open_chats \
                and not CRITICAL_SENTENCE.search(original) and not {k: v for k, v in row.payload.items() if k != META}:
            outcome, reason = 'archived', 'old_noncritical_dialogue'
        if outcome == 'active' and row.source_type in {'bank_receipt', 'research_report'}:
            # Cache only verified original source text, not a tampered mirror.
            try:
                original = self.memory.reread(owner, row.id, 'planner', row.conversation_id)['text']
            except BankError as error:
                if error.code not in {'MEMORY_SOURCE_INVALID', 'MEMORY_NOT_FOUND'}:
                    raise
                outcome, reason = 'quarantined', 'source_unavailable'
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        cache = current_state.get('curation', {})
        if outcome == 'active' and cache.get('source_digest') == fingerprint and cache.get('original_sha256') == original_hash:
            return 'active'
        summary, critical, _ = textrank(original, '', budget=650) if outcome == 'active' else ('', [], False)
        with self.db.write() as session:
            live = session.get(MemoryRecord, record_id)
            if live is None or live.owner_id != owner:
                return 'missing'
            if source_digest(live) != fingerprint:
                return 'active'  # next generation/sweep rereads changed content
            if outcome == 'archived' and live.conversation_id in self._open_conversations(session, owner):
                return 'active'  # a new task may have opened since the batch snapshot
            if not active(live) and outcome == 'active':
                return state(live).get('status', 'superseded')
            metadata = {**state(live), 'status': outcome, 'reason': reason}
            if outcome == 'active':
                # Exact scope+text+payload identity; never semantic merging.
                key = _identity(live)
                head = session.get(Document, key)
                previous = session.get(MemoryRecord, head.data['memory_id']) if head else None
                if previous and previous.id != live.id and _identity(previous) == key and active(previous) and previous.expires_at > now_iso():
                    metadata.update(status='duplicate', reason='exact_duplicate', canonical_id=previous.id)
                    outcome = 'duplicate'
                elif head:
                    head.data = {'memory_id': live.id}
                else:
                    session.add(Document(id=key, kind='memory_digest', owner_id=owner, data={'memory_id': live.id}))
            metadata['curation'] = {'source_digest': fingerprint, 'reviewed_at': now_iso(),
                'summary': summary, 'critical_originals': critical, 'original_sha256': original_hash,
                'authority': 'derived_context_only_not_authorization'}
            live.payload = {**live.payload, META: metadata}
        return outcome

    def status(self, owner):
        with self.db.read() as session:
            job = session.get(Document, _key(owner))
            values = dict(job.data) if job and job.owner_id == owner else {}
            counts = dict(session.execute(select(
                func.coalesce(func.json_extract(MemoryRecord.payload, '$.' + META + '.status'), 'active'),
                func.count()).where(MemoryRecord.owner_id == owner).group_by(
                    func.coalesce(func.json_extract(MemoryRecord.payload, '$.' + META + '.status'), 'active'))).all())
        return {'enabled': self.enabled, 'authority': 'context_only', 'counts': counts,
            **{k: values[k] for k in ('status', 'generation', 'processed_generation', 'attempt', 'error_code',
                'last_run_at', 'next_run_at', 'last_batch') if k in values}}

    async def maintain(self):
        delay = self.interval
        while True:
            await asyncio.sleep(delay)
            try:
                work = asyncio.create_task(asyncio.to_thread(self.run_pending))
                try:
                    result = await asyncio.shield(work)
                except asyncio.CancelledError:
                    await work
                    raise
                delay = 1 if result.get('more') else self.interval
            except Exception:
                delay = self.interval
