from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Step(EvalModel):
    kind: Literal["chat", "business", "finish", "incoming", "clock", "research", "select", "resume", "cancel",
                  "reject", "reconcile", "replay", "parallel", "no_mfa", "cross_user", "stale_edit", "tick",
                  "memory", "adapter", "model_fault", "edit", "restart", "grant_stale", "select_tamper",
                  "withdraw_before_due", "checkpoint", "api", "factor_attack", "replan", "fixture_event", "paired_submit"]
    label: str = ""
    text: str = ""
    category: str = ""
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    user: str = "demo"
    save: str = ""
    source: str = ""


class Check(EvalModel):
    source: str = "state"
    path: str
    op: Literal["eq", "in", "contains", "not_contains", "subset", "gte", "lte", "exists", "set_eq", "count"] = "eq"
    expected: Any = None
    description: str = ""


class Case(EvalModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    title: str
    group: Literal["business", "dialogue", "research", "security", "recovery"]
    channel: Literal["form", "natural_language", "research", "api_boundary"]
    category: str
    entry: str = ""
    balance_fen: int = Field(default=3_000_000, ge=0, le=100_000_000)
    risk_level: int = Field(default=2, ge=1, le=5)
    risk_valid_until: str = "2027-10-13"
    patches: list[dict] = Field(default_factory=list)
    setup: list[Step] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    # Scripted user only authorizes these operations with these important fields.
    allowed_writes: list[dict] = Field(default_factory=list)
    expected_outcome: Literal["complete", "blocked", "clarify", "reject", "research", "unknown"] = "complete"
    tags: list[str] = Field(default_factory=list)
    expected_behavior: str = ""
    criteria: list[str] = Field(default_factory=list)
    required_evidence_tools: list[str] = Field(default_factory=list)
    readiness: Literal["executable_development"] = "executable_development"
    gold_provenance: str = "Independent hand-designed state/financial assertions; not human double-labelled"


SUPPORTED_STEP_KINDS = {"chat", "business", "finish", "incoming", "clock", "research", "select", "resume", "cancel",
    "reconcile", "replay", "parallel", "cross_user", "tick", "adapter", "restart", "grant_stale", "select_tamper",
    "withdraw_before_due", "checkpoint", "api", "factor_attack", "edit", "no_mfa", "reject", "model_fault", "memory", "replan", "fixture_event", "paired_submit"}


def validate_executable(case):
    from bank_agent.bank_rules import DEFINITIONS
    for sequence in (case.setup, case.steps):
        labels = set()
        for step in sequence:
            if step.kind not in SUPPORTED_STEP_KINDS:
                raise ValueError("Unimplemented evaluation step: " + step.kind)
            if step.kind == "adapter" and step.params.get("mode") not in {"lost_response", "lookup_unavailable", "technical_failure", "forged_result"}:
                raise ValueError("Unsupported bank adapter fault mode")
            if step.label and (step.label == "last" or step.label in labels):
                raise ValueError(case.id + ": Duplicate or reserved evaluation step label")
            if step.source and step.source not in labels | {"last"}:
                raise ValueError("Step references a missing or future observation")
            if step.label:
                labels.add(step.label)
    sources = {s.label for s in case.steps if s.label} | {"state", "last"}
    for check in case.checks:
        if check.source not in sources:
            raise ValueError("Gold references an unknown observation")
        if check.path.startswith("receipts."):
            operation = check.path.removeprefix("receipts.")
            if operation not in DEFINITIONS and operation not in {"event.incoming", "event.payroll", "event.subscription", "event.redemption"}:
                raise ValueError("Gold references an unknown receipt operation")
    if any(s["operation"] not in DEFINITIONS for s in case.allowed_writes):
        raise ValueError("Unknown allowed business operation")


VARIANTS = {
    "SUPERVISOR": "当前应用：Supervisor＋账单研究、理财研究、业务办理、独立核验四个子Agent；六类规则服务",
    "S_AGENT": "统一领域提议＋单通才研究角色、单轮完整取证；共用约束解析与安全网关",
    "M_AGENT": "六领域规则提议＋双研究角色、单轮完整取证；与当前C比较证据策略",
    "A": "单规划/统一执行提议＋单研究角色、有界补查",
    "B": "六领域提议＋双研究角色、单轮完整取证",
    "C": "当前完整系统：六领域＋双角色定向补查＋共享记忆",
    "R_SINGLE": "受控研究消融：相同执行图与证据权限，单研究角色、有界补查",
    "R_DUAL": "受控研究消融：相同执行图与证据权限，双研究角色、有界补查",
    "R_ONESHOT": "受控取证消融：双研究角色、单轮完整证据；与有界补查比较成本",
    "M_NONE": "共享记忆消融：仅保留会话与受保护业务字段，不读取共享历史",
    "M_RAW": "共享记忆消融：相同ACL和检索，原文上下文，超预算证据回读",
    "M_COMPRESSED": "共享记忆消融：相同ACL和检索，TextRank与金融原句保留",
}
