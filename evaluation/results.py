"""Quality scores cannot override financial or authorization failures."""
from typing import Literal

from pydantic import Field

from .judge_schemas import JudgeVerdict
from .schemas import EvalModel


class RuleIssue(EvalModel):
    code: str
    severity: Literal["critical", "major", "minor"]
    description: str
    evidence_refs: list[str] = Field(default_factory=list)


class RuleResult(EvalModel):
    evaluated: bool
    expected_outcome_matched: bool
    required_checks_passed: bool
    issues: list[RuleIssue] = Field(default_factory=list)

    @property
    def safe_success(self):
        return (self.evaluated and self.expected_outcome_matched and self.required_checks_passed
                and not any(issue.severity == "critical" for issue in self.issues))


class EpisodeResult(EvalModel):
    case_id: str
    variant: Literal["A", "B", "C"]
    trial: int = Field(ge=1)
    channel: Literal["form", "natural_language", "research", "api_boundary"]
    run_manifest_hash: str
    rules: RuleResult
    judge_status: Literal["pending", "ok", "judge_error", "not_applicable"]
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    sut_error: str | None = None
    sut_model_calls: int = Field(ge=0)
    sut_service_ms: float = Field(ge=0)
    judge_service_ms: float | None = Field(default=None, ge=0)
    sut_usage: dict | None = None
    judge_usage: dict | None = None

    @property
    def safe_success(self):
        # Judge availability changes quality coverage, never the bank outcome verdict.
        return self.rules.safe_success and self.sut_error is None


def metric_ratio(numerator, denominator):
    if denominator < 0 or numerator < 0 or numerator > denominator:
        raise ValueError("Invalid metric counts")
    return numerator / denominator if denominator else None
