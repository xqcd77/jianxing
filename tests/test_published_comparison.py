"""Publication must preserve real arm identities and independently recompute scores."""

import copy
import json

import pytest

from evaluation.compare_results import DEFAULT_DATA, compare, summarize_variant


def test_current_published_results_are_recomputed_from_frozen_case_records():
    report = compare()
    single, current = report["variants"]
    assert (single["variant"], current["variant"]) == ("S_AGENT", "SUPERVISOR")
    assert (single["strict_passed"], current["strict_passed"]) == (32, 44)
    assert (single["weighted_score"], current["weighted_score"]) == (3.8451, 4.6462)
    assert report["deep_research_minus_single"]["strict_pass_percentage_points"] == 25
    assert report["model_call_caps"] == {"S_AGENT": 10, "SUPERVISOR": 16}
    assert report["equal_budget_comparison"] is False
    assert current["judge_valid"] == 48 and current["unresolved"] == 0
    assert current["dimension_means"]["evidence_reasoning"]["n"] == 24
    assert current["source"]["source_hash"] != single["source"]["source_hash"]


def test_unjudged_or_failed_business_record_cannot_be_published_as_strict_pass():
    data = json.loads((DEFAULT_DATA / "multi-agent-deep-research.json").read_text(encoding="utf-8"))
    invalid = copy.deepcopy(data)
    invalid["cases"][0]["objective_checks_pass"] = False
    with pytest.raises(ValueError, match="objective checks"):
        summarize_variant(invalid, "SUPERVISOR", "Current")
    invalid = copy.deepcopy(data)
    invalid["cases"][0]["judge_status"] = "invalid_response"
    with pytest.raises(ValueError, match="valid judge record"):
        summarize_variant(invalid, "SUPERVISOR", "Current")
