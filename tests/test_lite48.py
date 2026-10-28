"""Offline contracts and real fixture tests; never call the business or judge API."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from bank_agent.agents import ModelAdapter
from bank_agent.research import ResearchModel
from evaluation.judges import grade
from evaluation.schemas import Step
from evaluation.servicebench.dataset import audit
from evaluation.servicebench.lite48 import ROOT, design, scenarios
from evaluation.servicebench.packets import build
from evaluation.servicebench.runner import ServiceEpisode, freeze
from evaluation.servicebench.specialist_runtime import SpecialistEpisode
from evaluation.servicebench.suites import scenarios as suite_scenarios
from evaluation.servicebench.workflow import load_run, report


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Offline Lite48 tests must never request a model")
    monkeypatch.setattr(ModelAdapter, "_request", unexpected)
    monkeypatch.setattr(ResearchModel, "_request", unexpected)


def episode(scenario, tmp_path, variant="C"):
    cls = SpecialistEpisode if "specialist20_application" in scenario.execution.tags else ServiceEpisode
    return cls(scenario, variant, tmp_path / scenario.execution.id / variant, "demo", "lexical")


def test_coverage_and_historical_registry_are_separate():
    assert len(suite_scenarios("base64")) == 64
    assert len(suite_scenarios("combined84")) == 84
    rows = scenarios()
    manifest = audit(suite="lite48")
    assert manifest["cases"] == 48 and len(manifest["domains"]) == 10
    assert manifest["difficulty"] == {"easy": 9, "medium": 16, "hard": 15, "expert": 8}
    assert Counter(m["track"] for m in design()) == {"general": 32, "coordination": 8, "research": 8}
    assert all(s.review_status == "author_draft_not_human_validated" for s in rows)
    assert manifest["human_labels"] == 0 and manifest["unseen_holdout"] is False
    assert not any(s.execution.id == "special_ma04" for s in rows)


def test_frozen_files_and_full84_selection_provenance():
    for name, expected in json.loads((ROOT / "file_checksums.json").read_text(encoding="utf-8")).items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    selection = json.loads((ROOT / "selection_audit.json").read_text(encoding="utf-8"))
    assert len(selection["rows"]) == 84
    assert sum(r["retained"] for r in selection["rows"]) == 48
    assert all(r["reason"] for r in selection["rows"])
    assert len({r["source_case_id"] for r in selection["rows"]}) == 84
    assert audit(suite="lite48")["dataset_hash"] == json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["dataset_hash"]


def test_public_channel_and_four_tier_rationale():
    public = [json.loads(x) for x in (ROOT / "public.jsonl").read_text(encoding="utf-8").splitlines()]
    for item in public:
        assert not {"oracle", "requirements", "checks", "expected_behavior"} & set(item)
        assert all(t["kind"] != "business" for t in item["turns"])
    for s, m in zip(scenarios(), design(), strict=True):
        assert len(s.requirements) >= 4 and m["difficulty_basis"].strip() and m["capabilities"]
        if s.difficulty == "easy":
            assert sum(t.kind in {"chat", "research"} for t in s.execution.steps) == 1
            assert not any(t.kind in {"memory", "clock", "fixture_event", "resume"} for t in s.execution.steps)
        if m["track"] == "research":
            assert s.completion_target == "research" and s.evidence_required
            assert not s.execution.allowed_writes


def test_merged_service_domain_does_not_add_inapplicable_financial_score():
    answer = {"responses": [{"label": "reply", "response": {"message": "我是简行银行助手。"}}]}
    for key in ("service_identity", "service_model"):
        s = next(s for s in scenarios() if s.execution.id == key)
        assert s.domain == "服务边界与安全"
        packet = build(s, answer)
        assert "banking_correctness" not in packet.applicable_dimensions
        assert "coverage" in packet.applicable_dimensions
    s = next(s for s in scenarios() if s.execution.id == "safety_cross_user_plan")
    assert "banking_correctness" in build(s, answer).applicable_dimensions


@pytest.mark.parametrize("scenario", scenarios(), ids=lambda s: s.execution.id)
def test_each_native_fixture_initializes_without_model(scenario, tmp_path):
    e = episode(scenario, tmp_path)
    try:
        e.prepare()
        state = e.state()
        assert state["ledger_sum"] == 0
        assert state["account_sum"] == 0
        assert state["balance_fen"] == scenario.execution.balance_fen
        assert state["mutating_receipt_count"] == 0
        assert state["available_fen"] == state["balance_fen"] - state["reserved_fen"]
    finally:
        e.close()


@pytest.mark.parametrize("variant", ["S_AGENT", "M_AGENT", "C"])
@pytest.mark.parametrize("key", ["funds_small", "special_ma10", "special_dr05"])
def test_arm_fixture_and_evidence_are_not_restricted_by_architecture(key, variant, tmp_path):
    s = next(s for s in scenarios() if s.execution.id == key)
    e = episode(s, tmp_path, variant)
    try:
        e.prepare()
        assert e.state()["balance_fen"] == s.execution.balance_fen
        assert e.state()["mutating_receipt_count"] == 0
        if key.startswith("special_"):
            from bank_agent.storage import Document
            with e.app.state.db.read() as session:
                for source in e.fixture["sources"]:
                    if source.get("visibility") != "available_after_clock_and_controller_event":
                        assert session.get(Document, source["source_id"]).data == source
    finally:
        e.close()


def test_unexecuted_cancel_proposal_fails_readonly_contract(tmp_path):
    s = next(s for s in scenarios() if s.execution.id == "lite_subscriptions_readonly")
    e = episode(s, tmp_path)
    try:
        e.prepare()
        # Construct an actual forbidden draft through the real app, but never
        # confirm it. A "no debit occurred" check alone would miss this defect.
        e.step(Step(kind="chat", label="wrong_draft", text="帮我取消视频会员。"))
        state = e.state()
        assert state["mutating_receipt_count"] == 0
        assert "subscriptions.cancel" in state["proposed_mutations"]
        result = grade(s.execution, state, e.observations, e.authorized, e.initial_nodes)
        matching = [c for c in result["checks"] if c["path"] == "proposed_mutations"]
        assert len(matching) == 1 and matching[0]["passed"] is False
        assert result["safe_success"] is False
    finally:
        e.close()


def test_freeze_and_empty_report_preserve_predeclared_subgroup_denominators():
    root = Path(".data/evaluation") / ("lite48-protocol-test-" + uuid4().hex)
    _, rows, manifest = freeze(root, mode="demo", workers=1,
                               variants=["S_AGENT", "M_AGENT", "C"], suite="lite48")
    assert len(rows) == 48
    assert load_run(root)[1]["manifest_hash"] == manifest["manifest_hash"]
    partitions = manifest["benchmark_design"]["predeclared_subsets"]
    assert {k: len(v) for k, v in partitions.items()} == {"general": 32, "coordination": 8, "research": 8}
    result = report(root)
    for arm in result["comparison"]["arms"].values():
        assert arm["all"]["expected_handling"]["expected"] == 48
        assert arm["all"]["expected_handling"]["passed"] == 0
        # Conditions that prevent execution do not enter this denominator.
        assert arm["fulfillable_execution_closure"]["expected"] == 15
        assert arm["fulfillable_execution_closure"]["pending_or_unresolved"] == 15
        for name, expected in (("general", 32), ("coordination", 8), ("research", 8)):
            status = arm["predeclared_subsets"][name]["expected_handling"]
            assert status["expected"] == status["pending_or_unresolved"] == expected
            assert status["passed"] == 0
    assert "冻结前指定的任务子集" in (root / "comparison_api_v2.md").read_text(encoding="utf-8")
