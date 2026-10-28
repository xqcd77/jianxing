"""Architecture interventions and aggregation must not alter the score denominator."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from evaluation.servicebench.comparison import arm_metrics, paired
from evaluation.servicebench.runner import freeze
from evaluation.servicebench.workflow import load_run, report
from evaluation.variants import (
    OnePassResearchService, SingleOnePassResearchService, SingleResearchService,
)
from bank_agent.research import FOLLOWUP, ResearchService


def test_one_pass_arms_receive_same_full_evidence(monkeypatch):
    captured = []

    async def record(self, state):
        captured.append(state)
        return state

    monkeypatch.setattr(ResearchService, "_researcher", record)
    monkeypatch.setattr(SingleResearchService, "_researcher", record)
    state = {"role": "wealth", "active_roles": ["bills", "wealth"], "results": {}}
    single = object.__new__(SingleOnePassResearchService)
    multi = object.__new__(OnePassResearchService)
    asyncio.run(single._researcher(state))
    asyncio.run(multi._researcher(state))
    assert captured[0]["results"]["wealth"]["requested_reads"] == FOLLOWUP["wealth"]
    assert captured[1]["results"]["wealth"]["requested_reads"] == FOLLOWUP["wealth"]
    assert captured[0]["results"]["bills"]["requested_reads"] == FOLLOWUP["bills"]
    assert state["results"] == {}  # no mutation of another task's inputs


def row(case, status, score=4):
    return {"case_id": case, "trial": 1, "status": status, "domain": "research", "difficulty": "complex",
            "completion_target": "analyze", "resolution": "fulfilled", "invariant_safe": True,
            "response_present": True, "llm_exercised": True, "failure_modes": [], "objective_checks_pass": True,
            "judge_status": "judge_error" if status == "unresolved" else "ok", "goal_fulfilled": status == "passed",
            "scores": {} if status == "unresolved" else {"coverage": score}}


def test_missing_judge_retains_denominator_and_is_excluded_from_paired_scores():
    left = [row("a", "passed"), row("b", "unresolved")]
    right = [row("a", "failed", 3), row("b", "passed", 5)]
    metric = arm_metrics(left, [], 3)  # third episode not yet observed
    assert metric["expected_handling"]["passed"] == 1
    assert metric["expected_handling"]["pending_or_unresolved"] == 2
    assert metric["expected_handling"]["pass_rate_lower_bound"] == 1/3
    result = paired(left, right, {"a": "family_a", "b": "family_b"})
    assert result["resolved_pairs"] == 1
    assert result["unresolved_pairs_excluded"] == 1
    assert result["left_only_pass"] == 1
    assert result["quality_differences"]["coverage"] == {"n": 1, "right_minus_left_mean": -1}


def test_freeze_arm_identity_and_empty_report_are_isolated():
    root = Path('.data/evaluation') / ('architecture-protocol-test-' + uuid4().hex)
    variants = ["S_AGENT", "M_AGENT", "C"]
    _, _, manifest = freeze(root, ["service_identity"], "demo", 1, "lexical", 1, variants)
    assert load_run(root)[1]["variants"] == variants
    assert manifest["architecture_comparison"]["actual_compute_equalized"] is False
    result = report(root)
    for variant in variants:
        assert result["variants"][variant]["expected_handling"]["expected"] == 1
        assert result["variants"][variant]["expected_handling"]["passed"] == 0
    assert result["comparison"]["arms"]["C"]["research"]["hard_checks"]["rate"] is None
    with pytest.raises(ValueError):
        freeze(root, ["service_identity"], "demo", 1, "lexical", 1, ["C", "C"])
