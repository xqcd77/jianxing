from typing import Literal

from pydantic import Field, model_validator

from evaluation.schemas import Case, EvalModel, validate_executable


class Requirement(EvalModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    text: str = Field(min_length=12, max_length=700)
    required: bool = True
    weight: int = Field(default=1, ge=1, le=3)


class Scenario(EvalModel):
    execution: Case
    domain: str
    # Keep historical labels readable; new Lite48 uses four explicitly defined tiers.
    difficulty: Literal["simple", "medium", "complex", "easy", "hard", "expert"]
    family: str
    provenance: Literal["observed_failure_regression", "authored_diagnostic"]
    user_goal: str = Field(min_length=8)
    preconditions: str = Field(min_length=12)
    oracle: str = Field(min_length=30)
    acceptable_alternatives: str = Field(min_length=12)
    failure_modes: list[str] = Field(min_length=2)
    resolution: Literal["fulfilled", "clarified", "condition_explained", "safely_declined", "capability_gap"]
    completion_target: Literal["answer", "prepare", "execute", "research", "boundary"]
    requirements: list[Requirement] = Field(min_length=4, max_length=8)
    public_fixture: Literal["none", "curve", "curve_stale", "curve_unavailable", "fund_directory", "etf_directory"] = "none"
    evidence_required: bool = False
    review_status: Literal["author_draft_not_human_validated"] = "author_draft_not_human_validated"

    @model_validator(mode="after")
    def contract(self):
        validate_executable(self.execution)
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("Duplicate semantic requirement")
        if not any(s.kind in {"chat", "research"} for s in self.execution.steps):
            raise ValueError("A scenario must exercise a natural-language request")
        if any(s.kind == "business" for s in self.execution.steps):
            raise ValueError("Business forms cannot replace measured language understanding")
        if sum(r.required for r in self.requirements) < 3:
            raise ValueError("At least three task-specific mandatory requirements are needed")
        return self
