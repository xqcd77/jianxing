from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Domain(StrEnum):
    FUNDS = "funds"
    BILLS = "bills"
    WEALTH = "wealth"
    CARDS = "cards"
    SUBSCRIPTIONS = "subscriptions"
    LIFE = "life"


class DomainTask(StrictModel):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    domain: Domain
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    scheduled_for: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class ConversationAnswer(StrictModel):
    kind: Literal["identity", "model", "capabilities", "greeting", "thanks", "explanation", "other"]
    text: str = Field(min_length=1, max_length=1200)


class PublicInformationQuery(StrictModel):
    topic: Literal["risk_free_rate", "fund_products"]
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    tenor: str | None = Field(default=None, max_length=20)
    organization: str | None = Field(default=None, max_length=80)
    product_hint: str | None = Field(default=None, max_length=120)
    asset_type: Literal["fund", "etf"] = "fund"
    budget_fen: StrictInt | None = Field(default=None, ge=0, le=100_000_000)
    periodic_amount_fen: StrictInt | None = Field(default=None, gt=0, le=100_000_000)
    investment_horizon_months: StrictInt | None = Field(default=None, ge=1, le=1200)
    risk_level: StrictInt | None = Field(default=None, ge=1, le=5)
    channel: Literal['exchange', 'fund_platform'] | None = None


class AdditionalGoal(StrictModel):
    """A non-executable goal kept beside bank tasks; never a tool permission."""
    kind: Literal["public_information", "capability_gap", "clarification", "bank_task", "research"]
    title: str = Field(min_length=1, max_length=120)
    source_text: str = Field(default="", max_length=500)
    message: str = Field(default="", max_length=600)
    information_query: PublicInformationQuery | None = None
    pending_task: DomainTask | None = None
    research_query: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def match_query_to_kind(self):
        if (self.kind == 'public_information') != (self.information_query is not None):
            raise ValueError('公开资料目标必须有查询参数，其他目标不能附带资料查询')
        if (self.kind == 'bank_task') != (self.pending_task is not None):
            raise ValueError('待补办理目标必须保留对应的结构化任务')
        if (self.kind == 'research') != (self.research_query is not None):
            raise ValueError('研究目标必须保留原始研究需求')
        return self


class Intent(StrictModel):
    summary: str
    tasks: list[DomainTask] = Field(default_factory=list, max_length=24)
    clarification: str | None = None
    reject_reason: str | None = None
    clarification_kind: Literal["recipient", "amount", "product", "date", "risk_answers", "business", "other"] = "other"
    rejection_kind: Literal["business", "security"] = "business"
    answer: ConversationAnswer | None = None
    information_query: PublicInformationQuery | None = None
    additional_goals: list[AdditionalGoal] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def separate_answer_from_business(self):
        if self.answer and (self.tasks or self.clarification or self.reject_reason or self.information_query):
            raise ValueError("咨询答复不能同时包含办理任务、缺项追问或安全拒绝")
        if self.information_query and (self.tasks or self.clarification or self.reject_reason):
            raise ValueError("公开资料查询不能同时包含银行办理任务或缺项追问")
        return self


class NodeSpec(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    agent: str
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    scheduled_for: str | None = None
    reason: str = ""


class AgentProposal(StrictModel):
    agent: str
    nodes: list[NodeSpec]
    facts: list[dict[str, Any]] = Field(default_factory=list)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    model_id: str | None = Field(default=None, max_length=40, pattern=r'^[a-z][a-z0-9_-]*$')


class LoginRequest(StrictModel):
    username: str
    password: str


class ApprovalRequest(StrictModel):
    node_id: str
    fingerprint: str
    decision: Literal["approve", "reject"] = "approve"
    otp: str | None = None
    demo_challenge_id: str | None = Field(default=None, min_length=1, max_length=80)


class PayrollRequest(StrictModel):
    event_id: str = Field(min_length=1, max_length=100)
    amount_fen: int = Field(gt=0, le=100_000_000)


class ClockRequest(StrictModel):
    days: int = Field(ge=0, le=366)


class PlanEdit(StrictModel):
    node_id: str
    params: dict[str, Any]
    scheduled_for: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class ProtectedField(StrictModel):
    name: str
    value: Any
    source_id: str
    source_type: Literal["user_message", "bank_state", "bank_receipt", "draft", "product_snapshot", "user_preference", "research_report"]


class ProtectedContext(StrictModel):
    currency: Literal["CNY"] = "CNY"
    plan_id: str | None = None
    plan_version: int | None = None
    fields: list[ProtectedField] = Field(default_factory=list)
    negative_constraints: list[str] = Field(default_factory=list)
    forbidden_operations: list[str] = Field(default_factory=list)
    receipt_refs: list[str] = Field(default_factory=list)


class MemorySearch(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    role: Literal["planner", "reviewer", "funds", "bills", "wealth", "cards", "subscriptions", "life"] = "planner"
    conversation_id: str | None = None


class PreferenceRequest(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    domains: list[Domain] = Field(min_length=1, max_length=6)
    key: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,79}$")
    expected_version: int | None = Field(default=None, ge=0)


class ReplanRequest(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    nodes: list[NodeSpec] = Field(min_length=1, max_length=64)


class BatchApprovalRequest(StrictModel):
    expected_version: int = Field(ge=1)
    manifest_fingerprint: str = Field(min_length=64, max_length=64)
