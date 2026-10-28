"""External judge has evidence and rubrics, never a bank connection or tools."""
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from .schemas import EvalModel


class Evidence(EvalModel):
    id: str = Field(min_length=1, max_length=100)
    authority: Literal["bank_snapshot", "bank_receipt", "product_disclosure", "user_declared", "independent_gold"]
    version: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)


class Criterion(EvalModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=500)
    critical: bool = False


class JudgePacket(EvalModel):
    case_id: str
    user_goal: str = Field(min_length=1, max_length=4000)
    expected_behavior: str = Field(min_length=1, max_length=3000)
    candidate_output: str = Field(min_length=1, max_length=30000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=40)
    criteria: list[Criterion] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_ids(self):
        for rows in (self.evidence, self.criteria):
            if len({r.id for r in rows}) != len(rows):
                raise ValueError("Duplicate evidence or criterion IDs")
        return self


class DimensionScore(EvalModel):
    score: StrictInt = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=400)


class FiveScores(EvalModel):
    coverage: DimensionScore
    banking_correctness: DimensionScore
    evidence_reasoning: DimensionScore
    actionability: DimensionScore
    clarity: DimensionScore


class CriterionFinding(EvalModel):
    criterion_id: str
    verdict: Literal["met", "not_met", "insufficient_evidence"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=300)


class JudgeVerdict(EvalModel):
    # No business success flag: only deterministic judges decide safe task success.
    scores: FiveScores
    findings: list[CriterionFinding] = Field(min_length=1, max_length=30)
    summary: str = Field(min_length=1, max_length=400)

    def validate_against(self, packet: JudgePacket):
        wanted = {c.id for c in packet.criteria}
        actual = [f.criterion_id for f in self.findings]
        if len(actual) != len(set(actual)) or set(actual) != wanted:
            raise ValueError("Judge omitted, duplicated or invented a criterion")
        sources = {e.id for e in packet.evidence}
        if any(set(f.evidence_ids) - sources for f in self.findings):
            raise ValueError("Judge invented evidence references")
        return self
