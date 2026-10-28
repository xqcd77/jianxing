"""Server-built role handoffs. Typed facts/proposals, never private reasoning."""
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .errors import BankError
from .research_tools import digest
from .research_schemas import ResearchBrief
from .runtime import append_event
from .runtime_context import task_state
from .schemas import Intent, NodeSpec, StrictModel
from .storage import Document


Role = Literal['supervisor', 'bills_research', 'wealth_research', 'business', 'verifier']


class Claim(StrictModel):
    id: str
    kind: str
    text: str
    values: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class DispatchContent(StrictModel):
    kind: Literal['dispatch'] = 'dispatch'
    targets: list[Role]


class BriefContent(StrictModel):
    kind: Literal['research_brief'] = 'research_brief'
    brief: ResearchBrief
    active_roles: list[Literal['bills', 'wealth']]
    research_details: dict = Field(default_factory=dict)


class SpecialistContent(StrictModel):
    kind: Literal['specialist'] = 'specialist'
    claims: list[Claim]
    focus_claim_ids: list[str] = Field(default_factory=list)


class SynthesisContent(StrictModel):
    kind: Literal['synthesis'] = 'synthesis'
    method: str
    findings: list[Claim] = Field(default_factory=list)
    calculations: list[dict] = Field(default_factory=list)


class BusinessContent(StrictModel):
    kind: Literal['business'] = 'business'
    intent: Intent
    nodes: list[NodeSpec] = Field(default_factory=list)


class ReviewContent(StrictModel):
    kind: Literal['review'] = 'review'
    passed: bool
    method: str
    issues: list[dict] = Field(default_factory=list)
    input_digest: str

    @model_validator(mode='after')
    def checked_issues(self):
        from .supervisor_agents import ReviewIssue
        self.issues = [ReviewIssue.model_validate(i).model_dump(mode='json') for i in self.issues]
        if self.passed and any(i['blocking'] for i in self.issues):
            raise ValueError('blocking issues cannot pass')
        return self


Content = Annotated[DispatchContent | BriefContent | SpecialistContent | SynthesisContent |
                    BusinessContent | ReviewContent, Field(discriminator='kind')]
KINDS = {'supervisor': {'dispatch', 'research_brief', 'synthesis'},
         'bills_research': {'specialist'}, 'wealth_research': {'specialist'},
         'business': {'business'}, 'verifier': {'review'}}
READERS = {'supervisor': set(KINDS), 'verifier': set(KINDS),
           'bills_research': {'supervisor', 'bills_research'},
           'wealth_research': {'supervisor', 'wealth_research'},
           'business': {'supervisor', 'business'}}


class EvidenceRef(StrictModel):
    id: str
    sha256: str
    as_of: str
    source_ids: list[str] = Field(default_factory=list)


class RoleReturn(StrictModel):
    protocol_version: Literal[1] = 1
    authority: Literal['proposal_or_read_only_not_authorization'] = 'proposal_or_read_only_not_authorization'
    owner_id: str
    conversation_id: str
    task_id: str
    role: Role
    stage: str
    revision: int = Field(ge=0)
    source_fingerprint: str
    status: Literal['complete', 'partial', 'blocked']
    content: Content
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    next_reads: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def checked_handoff(self):
        from .research_tools import READS
        if self.content.kind not in KINDS[self.role]:
            raise ValueError('role/content mismatch')
        allowed = {r.id for r in self.evidence_refs}
        allowed.update(s for r in self.evidence_refs for s in r.source_ids)
        claims = getattr(self.content, 'claims', getattr(self.content, 'findings', []))
        if any(set(c.evidence_ids) - allowed for c in claims):
            raise ValueError('unknown evidence reference')
        if self.content.kind == 'review' and any(set(i['evidence_ids']) - allowed for i in self.content.issues):
            raise ValueError('unknown review evidence')
        if self.content.kind == 'specialist':
            if set(self.content.focus_claim_ids) - {c.id for c in claims}:
                raise ValueError('unknown claim')
            role = self.role.removesuffix('_research')
            if set(self.next_reads) - set(READS[role]):
                raise ValueError('read outside role boundary')
        elif self.next_reads:
            raise ValueError('only specialists may request domain reads')
        return self


class RoleReturnStore:
    """Internal immutable handoffs; not a memory search or an executable tool."""
    def __init__(self, db):
        self.db = db

    def record(self, *, owner_id, conversation_id, task_id, role, stage, revision=0,
               source_fingerprint='', status='complete', content, evidence=(),
               open_items=(), next_reads=()):
        actor = task_state.get()
        if actor.owner_id and (actor.owner_id != owner_id or
                actor.conversation_id and actor.conversation_id != conversation_id):
            raise BankError('ROLE_RETURN_SCOPE', '角色交接范围不一致', 403)
        refs = []
        for ev in evidence:
            if digest(ev['data']) != ev['sha256']:
                raise BankError('ROLE_RETURN_EVIDENCE', '角色交接证据校验失败', 409)
            refs.append({k: deepcopy(ev.get(k, [] if k == 'source_ids' else ''))
                         for k in ('id', 'sha256', 'as_of', 'source_ids')})
        packet = RoleReturn(owner_id=owner_id, conversation_id=conversation_id, task_id=task_id,
            role=role, stage=stage, revision=revision, source_fingerprint=source_fingerprint,
            status=status, content=deepcopy(content), evidence_refs=refs,
            open_items=list(open_items), next_reads=list(dict.fromkeys(next_reads)))
        data = packet.model_dump(mode='json')
        return_id = 'role_return_' + digest(data)
        with self.db.write() as session:
            if session.get(Document, return_id) is None:
                session.add(Document(id=return_id, kind='role_return', owner_id=owner_id, data=data))
                append_event(session, 'role.returned', owner_id, role=role, task_id=task_id,
                    details={'role_return_id': return_id, 'stage': stage, 'version': revision,
                             'status': status, 'candidate_count': len(refs), 'hit_count': len(open_items)})
        return return_id

    def read(self, return_id, *, owner_id, conversation_id, reader, task_id=None):
        actor = task_state.get()
        if actor.owner_id and (actor.owner_id != owner_id or
                actor.conversation_id and actor.conversation_id != conversation_id):
            raise BankError('ROLE_RETURN_SCOPE', '角色交接范围不一致', 403)
        with self.db.read() as session:
            row = session.get(Document, return_id)
            if row is None or row.kind != 'role_return' or row.owner_id != owner_id:
                raise BankError('ROLE_RETURN_NOT_FOUND', '角色交接记录不存在', 404)
            data = deepcopy(row.data)
        if data.get('conversation_id') != conversation_id or data.get('role') not in READERS.get(reader, set()):
            raise BankError('ROLE_RETURN_SCOPE', '无权读取此角色交接记录', 403)
        if task_id is not None and data.get('task_id') != task_id:
            raise BankError('ROLE_RETURN_SCOPE', '角色交接任务不一致', 403)
        if return_id != 'role_return_' + digest(data):
            raise BankError('ROLE_RETURN_CHANGED', '角色交接内容已变化', 409)
        return RoleReturn.model_validate(data)

    def model_input(self, return_id, **scope):
        packet = self.read(return_id, **scope)
        # No actor identity, private histories or model rationale in model context.
        return {k: v for k, v in packet.model_dump(mode='json').items() if k in {
            'protocol_version', 'authority', 'role', 'stage', 'revision', 'source_fingerprint',
            'status', 'content', 'evidence_refs', 'open_items', 'next_reads'}}
