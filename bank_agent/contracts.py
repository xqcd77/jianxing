"""Public financial response contracts used by OpenAPI and browser type generation."""

from typing import Any, Literal

from pydantic import Field, StrictInt

from .schemas import StrictModel


class FundsSnapshot(StrictModel):
    balance_fen: StrictInt
    reserved_fen: StrictInt
    available_fen: StrictInt


class BusinessIssue(StrictModel):
    code: str
    message: str
    category: Literal["business", "security", "technical"] | None = None
    next_step: str | None = None
    node_id: str | None = None
    future: bool | None = None
    shortfall_fen: StrictInt | None = None


class PreviewStep(StrictModel):
    node_id: str
    local_id: str
    operation: str
    params: dict[str, Any]
    tier: Literal["GREEN", "YELLOW", "RED", "UNKNOWN"]
    outflow_fen: StrictInt
    inflow_fen: StrictInt
    reserve_change_fen: StrictInt
    future: bool
    after: FundsSnapshot
    reference_bound: bool
    blocked: bool = False
    scheduled_on: str | None = None


class PressureEvent(StrictModel):
    date: str
    label: str
    delta_fen: StrictInt
    projected_available_fen: StrictInt
    source: str


class CashflowPressure(StrictModel):
    horizon_end: str
    minimum_available_fen: StrictInt
    shortfall_fen: StrictInt
    shortfall_on: str | None
    complete: bool
    events: list[PressureEvent]
    assumptions: list[str]


class FundingPreview(StrictModel):
    fingerprint: str
    before: FundsSnapshot
    after: FundsSnapshot
    immediate_after: FundsSnapshot
    required_fen: StrictInt
    outflow_fen: StrictInt
    new_reserved_fen: StrictInt
    shortfall_fen: StrictInt
    ready: bool
    complete_projection: bool
    blockers: list[BusinessIssue]
    steps: list[PreviewStep]
    holds: list[dict[str, Any]]
    deferred_inflows: list[dict[str, Any]]
    known_future_debits_fen: StrictInt
    simulated_recurring_debits_fen: StrictInt
    event_warnings: list[str]
    after_known_obligations_available_fen: StrictInt
    expected_income_counted_as_available: bool
    business_time: str
    note: str
    pressure: CashflowPressure


class RiskReason(StrictModel):
    code: str
    message: str
    sources: list[str]


class ProductRiskEvidence(StrictModel):
    name: str
    risk_level: StrictInt
    minimum_fen: StrictInt
    lock_days: StrictInt
    redeem_business_days: StrictInt
    disclosure_verified: bool
    source_version: StrictInt | None
    notes: list[str]


class RiskDecision(StrictModel):
    authorization_tier: Literal["GREEN", "YELLOW", "RED", "DYNAMIC"]
    status: Literal["READ_ONLY", "CONFIRM", "VERIFY", "BLOCKED", "VERIFYING", "WAITING", "FINISHED"]
    title: str
    reasons: list[RiskReason]
    consequences: list[str]
    controls: list[str]
    permitted_next_actions: list[Literal["edit", "fund", "assess", "security", "reconcile", "refresh", "support"]]
    evaluated_at: str
    plan_version: StrictInt
    fingerprint: str
    facts_digest: str
    product: ProductRiskEvidence | None = None


class ConfirmationItem(StrictModel):
    node_id: str
    operation: str
    params: dict[str, Any]
    tier: Literal["YELLOW"]
    display_amount_fen: StrictInt | None


class ConfirmationBatch(StrictModel):
    available: bool
    items: list[ConfirmationItem]
    fingerprint: str
    plan_version: StrictInt
    message: str


class PlanNode(StrictModel):
    id: str
    local_id: str
    agent: str
    operation: str
    params: dict[str, Any]
    depends_on: list[str]
    after: list[str]
    status: Literal["READY", "WAIT_CONFIRM", "WAIT_MFA", "WAIT_TIME", "WAIT_INPUT", "WAIT_HUMAN", "FAILED", "UNKNOWN", "SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"]
    tier: Literal["GREEN", "YELLOW", "RED", "DYNAMIC"]
    scheduled_for: str | None
    fingerprint: str
    result: dict[str, Any] | None
    error: BusinessIssue | None
    reason: str
    risk: RiskDecision


class SupportTimelineEntry(StrictModel):
    at: str
    text: str


class SupportCaseRecord(StrictModel):
    id: str
    plan_id: str
    node_id: str | None
    status: Literal["PENDING", "ACTION_REQUIRED", "FOLLOW_UP", "RESOLVED", "CLOSED"]
    reason_code: str
    title: str
    message: str
    next_step: str
    created_at: str
    updated_at: str | None
    followup_plan_id: str | None
    timeline: list[SupportTimelineEntry]


class AdvisoryBudgetRule(StrictModel):
    basis: Literal['available_after_unfinished_plan_outflows']
    fraction: str = Field(pattern=r'^(?:0\.\d+|1(?:\.0+)?)$')
    capital_fen: StrictInt | None = Field(default=None, ge=0)


class CompletedPlanReference(StrictModel):
    plan_id: str
    message: str


class RequestGoal(StrictModel):
    id: str
    kind: Literal["bank_operation", "public_information", "capability_gap", "clarification", "bank_task", "research"]
    title: str
    status: str
    node_id: str | None = None
    query: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    notice: str | None = None
    source_text: str | None = None
    revision: int = 1
    slots: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    operation: str | None = None
    pending_task: dict[str, Any] | None = None
    research_query: str | None = None
    budget_rule: AdvisoryBudgetRule | None = None


class PlanSnapshot(StrictModel):
    id: str
    thread_id: str
    conversation_id: str
    summary: str
    status: str
    version: StrictInt = Field(ge=1)
    review: dict[str, Any]
    nodes: list[PlanNode]
    execution_mode: Literal["sandbox"]
    available_fen: StrictInt
    security_locked: bool
    assistant_message: str
    funding_preview: FundingPreview
    confirmation_batch: ConfirmationBatch
    support_cases: list[SupportCaseRecord] = Field(default_factory=list)
    request_goals: list[RequestGoal] = Field(default_factory=list)
    goal_revision: int | None = None
    completed_history: list[CompletedPlanReference] = Field(default_factory=list)


class PublicUser(StrictModel):
    id: str
    username: str
    name: str
    locked: bool
    risk_level: StrictInt
    demo_admin: bool


class BusinessChoice(StrictModel):
    value: str
    label: str
    detail: str | None = None


class BusinessField(StrictModel):
    name: str
    label: str
    kind: str
    required: bool
    choices: list[BusinessChoice] | None = None
    source: str | None = None
    help: str | None = None
    default: Any = None
    default_key: str | None = None
    show_when: dict[str, list[str]] | None = None
    empty_message: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    placeholder: str | None = None


class BusinessAction(StrictModel):
    id: str
    label: str
    description: str
    operation: str | None
    fields: list[BusinessField]
    tier: Literal["GREEN", "YELLOW", "RED", "DYNAMIC"]
    available: bool
    mode: str = "operation"
    children: list["BusinessAction"] | None = None
    fixed: dict[str, Any] | None = None
    capability: dict[str, Any] | None = None


class BusinessCategory(StrictModel):
    id: str
    label: str
    icon: str
    description: str
    actions: list[BusinessAction]
