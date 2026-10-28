"""Typed research constraints. Financial fields never travel through prose compression."""

from typing import Literal

from pydantic import Field, StrictInt

from .schemas import StrictModel


class DeclaredExpense(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount_fen: StrictInt = Field(gt=0, le=100_000_000)


class ResearchBrief(StrictModel):
    topic: Literal["bills", "wealth", "combined"] = "combined"
    period: str | None = Field(default=None, pattern=r"^\d{4}(?:-\d{2})?$")
    days: StrictInt = Field(default=30, ge=1, le=366)
    amount_fen: StrictInt | None = Field(default=None, gt=0, le=100_000_000)
    reserve_fen: StrictInt = Field(default=0, ge=0, le=100_000_000)
    emergency_fen: StrictInt = Field(default=0, ge=0, le=100_000_000)
    needed_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    expenses: list[DeclaredExpense] = Field(default_factory=list, max_length=12)
    analysis_only: bool = False
    forbid_purchase: bool = False
    excluded_product_ids: list[str] = Field(default_factory=list, max_length=16)
    negative_constraints: list[str] = Field(default_factory=list, max_length=20)


class ResearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=100)
    # A form can supply exact, user-entered fields; otherwise the supervisor extracts them.
    constraints: ResearchBrief | None = None


class ResearchDecision(StrictModel):
    focus_claim_ids: list[str] = Field(default_factory=list, max_length=24)
    # These names belong to a read-only broker, not the bank operation catalog.
    reads: list[Literal["transaction_details", "stress", "product_terms"]] = Field(default_factory=list, max_length=3)
    # Kept in the internal trace. User-facing conclusions come from verified claims.
    rationale: str = Field(default="", max_length=1000)


class ResearchSelection(StrictModel):
    option_id: str = Field(min_length=1, max_length=80)
    expected_version: StrictInt = Field(ge=1)


class BillsResearchDecision(ResearchDecision):
    focus_claim_ids: list[str] = Field(default_factory=list, max_length=8)
    reads: list[Literal["transaction_details", "stress"]] = Field(default_factory=list, max_length=2)
    rationale: str = Field(default="", max_length=300)


class WealthResearchDecision(ResearchDecision):
    focus_claim_ids: list[str] = Field(default_factory=list, max_length=8)
    reads: list[Literal["product_terms"]] = Field(default_factory=list, max_length=1)
    rationale: str = Field(default="", max_length=300)
