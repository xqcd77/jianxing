import copy
import json
from pathlib import Path
import uuid

import pytest

from evaluation.dataset import core35, export_core35
from evaluation.judges import grade, values
from evaluation.packets import build_packet
from evaluation.runner import Episode, isolated_directory, run_case
from evaluation.config import JudgeSettings
from evaluation.report import summarize
from evaluation.schemas import Step
from evaluation.experiment import digest


def test_public_gold_separation(tmp_path):
    result = export_core35(tmp_path)
    public = [json.loads(line) for line in (tmp_path / "public/core35.jsonl").read_text(encoding="utf-8").splitlines()]
    gold = [json.loads(line) for line in (tmp_path / "gold/core35.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result["cases"] == len(public) == len(gold) == 35
    assert digest(public) != digest(gold)
    assert {p["id"] for p in public} == {g["id"] for g in gold}
    assert all("checks" not in p and "expected_behavior" not in p for p in public)
    assert all(g["checks"] for g in gold)
    assert not result["human_double_labelled"]


def test_projection_literal_operation_and_missing_fields():
    assert values({"receipts": {"funds.transfer": 1}}, "receipts.funds.transfer") == 1
    assert values({"nodes": [{"error": None}]}, "nodes.*.error") == [None]
    assert values({}, "balance_fen") is None


def test_yellow_prerequisite_read_is_confirmable_but_cancellation_is_not():
    episode = Episode.__new__(Episode)
    episode.setup_mode = False
    episode.case = next(c for c in core35() if c.id == "nl_subscriptions_03")
    assert episode.user_allows({"tier": "YELLOW", "operation": "subscriptions.query", "params": {}})
    assert not episode.user_allows({"tier": "YELLOW", "operation": "subscriptions.cancel",
                                   "params": {"subscription_id": "S_MUSIC", "scope": "both"}})


def test_judge_scheduling_budget_does_not_expose_credentials():
    assert "api_key" not in JudgeSettings(api_key="private-test-value", concurrency=6).public()
    with pytest.raises(ValueError):
        JudgeSettings(concurrency=7)


def test_partial_report_preserves_pending_and_manifest_binding(tmp_path):
    manifest = {"manifest_hash": "frozen", "variants": ["A", "B", "C"], "cases": ["case_1", "case_2"],
                "trials": 1, "sut_model": "test-model", "judge": {"model": "independent-judge"}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for variant in "ABC":
        target = tmp_path / variant / "case_1" / "trial_1"
        target.mkdir(parents=True)
        row = {"manifest_hash": "frozen", "variant": variant, "case_id": "case_1", "trial": 1,
               "title": "fixture", "group": "dialogue", "channel": "natural_language", "llm_exercised": True,
               "model_call_count": 1, "sut_wall_ms": 10, "sut_error": None,
               "rules": {"safe_success": variant != "B", "checks": [], "policy_failures": []}, "usage": None}
        (target / "episode.json").write_text(json.dumps(row), encoding="utf-8")
    result = summarize(tmp_path)
    assert result["expected_episodes"] == 6 and result["completed_episodes"] == 3
    assert result["variants"]["A"]["judge_pending"] == 1
    assert result["variants"]["A"]["quality_means"] == {}
    assert result["variants"]["A"]["sut_usage"]["total_tokens_missing_episodes"] == 1
    target = tmp_path / "A/case_1/trial_1/episode.json"
    wrong = json.loads(target.read_text(encoding="utf-8"))
    wrong["manifest_hash"] = "other-run"
    target.write_text(json.dumps(wrong), encoding="utf-8")
    with pytest.raises(ValueError):
        summarize(tmp_path)


def test_financial_directory_rejects_workbench_and_replay(tmp_path):
    with pytest.raises(ValueError):
        isolated_directory(tmp_path, "C", "case", 1)
    root = Path(".data/evaluation") / ("test_isolation_" + uuid.uuid4().hex)
    path = isolated_directory(root, "C", "case", 1)
    path.mkdir(parents=True)
    (path / "bank.sqlite3").touch()
    with pytest.raises(ValueError):
        isolated_directory(root, "C", "case", 1)


def test_no_op_does_not_pass_reminder_task():
    case = next(c for c in core35() if c.id == "nl_subscriptions_03")
    case = case.model_copy(update={"steps": [Step(kind="checkpoint", label="unchanged")]})
    root = Path(".data/evaluation") / ("test_no_op_" + uuid.uuid4().hex)
    result = run_case(case, "C", root, model_mode="demo", memory_backend="lexical")
    assert not result["rules"]["safe_success"]
    assert any(c["path"] == "receipts.subscriptions.remind" and not c["passed"] for c in result["rules"]["checks"])


def test_real_api_fixture_and_financial_mutants():
    case = next(c for c in core35() if c.id == "nl_funds_01")
    root = Path(".data/evaluation") / ("test_runner_" + uuid.uuid4().hex)
    result = run_case(case, "C", root, model_mode="demo", memory_backend="lexical")
    assert result["rules"]["safe_success"]
    state = result["state"]
    receipt = next(r for r in state["new_receipts"] if r["operation"] == "funds.transfer")
    node_id = receipt["key"][5:]
    authorized = {node_id: {"mfa": False, "tier": "YELLOW", "user_id": "u_demo",
        "fingerprint": next(g["fingerprint"] for g in state["grants"] if g["node_id"] == node_id)}}
    assert grade(case, state, result["observations"], authorized, set())["safe_success"]
    bad = copy.deepcopy(state)
    bad["grants"][0]["fingerprint"] = "different-plan"
    assert grade(case, bad, result["observations"], authorized, set())["policy_failures"]
    bad = copy.deepcopy(state)
    bad["balance_fen"] += 1
    assert not grade(case, bad, result["observations"], authorized, set())["safe_success"]
    bad = copy.deepcopy(state)
    bad["ledger_sum"] = 1
    assert grade(case, bad, result["observations"], authorized, set())["policy_failures"]
    packet = build_packet(case, result).model_dump_json()
    assert "validated_output" not in packet and "mimo-v2.6-pro" not in packet
    assert "模型调用" not in packet and "sk-" not in packet
    ui_packet = build_packet(case, result, plan_ui=True).candidate_output
    assert "界面资金预览（元）" in ui_packet
    assert "权限级别" in ui_packet and "500.00" in ui_packet
    assert "otp" not in ui_packet and "demo_challenge_id" not in ui_packet


def test_plan_ui_preserves_mfa_before_completion_for_current_and_legacy_traces():
    case = next(c for c in core35() if c.id == "nl_funds_03")
    root = Path(".data/evaluation") / ("test_timeline_" + uuid.uuid4().hex)
    result = run_case(case, "C", root, model_mode="demo", memory_backend="lexical")
    assert result["rules"]["safe_success"]
    for legacy in (False, True):
        observed = copy.deepcopy(result)
        if legacy:
            for turn in observed["responses"]:
                turn.pop("trace_event_index")
        turns = json.loads(build_packet(case, observed, plan_ui=True).candidate_output)
        waiting = next(i for i, t in enumerate(turns) if t.get("实际确认后的界面更新", {}).get("界面状态") == "等待强验证")
        done = next(i for i, t in enumerate(turns) if t.get("轮次") == "done")
        assert waiting < done
        assert turns[done]["界面状态"] == "全部完成"
