import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from evaluation.blueprints import inventory, validate_inventory
from evaluation.config import JudgeSettings
from evaluation.judge_schemas import Criterion, Evidence, JudgePacket, JudgeVerdict
from evaluation.llm_judge import JudgeError, MiMoJudge
from evaluation.results import EpisodeResult, RuleIssue, RuleResult, metric_ratio
from evaluation.statistics import mcnemar_exact, paired_bootstrap, paired_dz, wilson_interval
from evaluation.trace import Trace, scrub
from evaluation.variants import create_variant_app


def packet():
    return JudgePacket(case_id="case_1", user_goal="给妈妈500元", expected_behavior="余额不足时不扣款",
        candidate_output="当前余额不足，这笔钱未转出。请补充500元后继续确认。",
        evidence=[Evidence(id="e1", authority="bank_snapshot", version="v1", text="可用余额0元，没有回执。")],
        criteria=[Criterion(id="c1", text="解释余额不足与尚未转出", critical=True)])


def verdict(score=5):
    return {"scores": {k: {"score": score, "reason": "余额、未转出及下一步与证据一致"}
                       for k in ["coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"]},
            "findings": [{"criterion_id": "c1", "verdict": "met", "evidence_ids": ["e1"], "reason": "证据显示可用为0"}],
            "summary": "条件与下一步说明清楚"}


def response(data=None, reason="stop"):
    return {"model": "mimo-v2.6-pro", "choices": [{"finish_reason": reason, "message": {
        "content": json.dumps(data if data is not None else verdict(), ensure_ascii=False),
        "reasoning_content": "private reasoning must not be retained", "tool_calls": None}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                  "completion_tokens_details": {"reasoning_tokens": 20}}}


def test_inventory_has_exact_stage_sizes_and_six_categories():
    rows = inventory()
    result = validate_inventory(rows)
    assert result["total"] == 100 and result["core35"] == 35
    assert not result["executable"] and not result["human_reviewed"]
    core_ids = {r.id for r in rows if r.core35}
    assert len(core_ids) == 35 and all(r.readiness == "designed_not_executable" for r in rows)


def test_inventory_does_not_accept_duplicate_or_missing_tasks():
    with pytest.raises(ValueError):
        validate_inventory(inventory()[:-1])
    rows = inventory()
    rows[-1] = rows[0]
    with pytest.raises(ValueError):
        validate_inventory(rows)


def test_judge_settings_hide_key_and_do_not_redirect_it():
    settings = JudgeSettings(api_key="test-only-key")
    assert "test-only-key" not in repr(settings) and "api_key" not in settings.public()
    for target in ["http://api.xiaomimimo.com/v1", "https://attacker.example/v1",
                   "https://api.xiaomimimo.com/v1?secret=x", "https://user:pass@api.xiaomimimo.com/v1"]:
        with pytest.raises(ValueError):
            JudgeSettings(base_url=target)


@pytest.mark.parametrize("score", [0, 6, True, "5", 4.5])
def test_judge_score_must_be_integer_in_range(score):
    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate(verdict(score))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "fake_reference", "fake_criterion"])
def test_judge_cannot_omit_or_invent_findings(mutation):
    data = verdict()
    if mutation == "missing":
        data["findings"] = []
    elif mutation == "duplicate":
        data["findings"].append(data["findings"][0])
    elif mutation == "fake_reference":
        data["findings"][0]["evidence_ids"] = ["invented"]
    else:
        data["findings"][0]["criterion_id"] = "invented"
    with pytest.raises(ValueError):
        JudgeVerdict.model_validate(data).validate_against(packet())


def test_mimo_request_has_no_tools_no_deepseek_effort_and_no_private_reasoning():
    captured = []
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        assert request.headers["api-key"] == "test-only-key"
        assert body["model"] == "mimo-v2.6-pro" and body["thinking"] == {"type": "enabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and "reasoning_effort" not in body and "temperature" not in body
        return httpx.Response(200, json=response())
    result = asyncio.run(MiMoJudge(JudgeSettings(api_key="test-only-key"), httpx.MockTransport(handler)).grade(packet()))
    assert result.verdict.scores.coverage.score == 5
    assert len(captured) == 1 and result.attempts[0]["usage"]["total_tokens"] == 150
    assert "private reasoning" not in str(result)


def test_format_retry_is_bounded_and_keeps_invalid_call_usage():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        data = verdict(9) if len(calls) == 1 else verdict()
        return httpx.Response(200, json=response(data))
    result = asyncio.run(MiMoJudge(JudgeSettings(api_key="test-only-key", rpm=100),
                                  httpx.MockTransport(handler)).grade(packet()))
    assert len(calls) == 2 and [m["status"] for m in result.attempts] == ["format_error", "ok"]
    assert sum(m["usage"]["total_tokens"] for m in result.attempts) == 300
    assert "score" not in calls[1]["messages"][1]["content"]


def test_auth_failure_is_recorded_without_retry_or_sensitive_body():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"secret": "test-only-key"})
    with pytest.raises(JudgeError) as caught:
        asyncio.run(MiMoJudge(JudgeSettings(api_key="test-only-key"), httpx.MockTransport(handler)).grade(packet()))
    assert caught.value.code == "JUDGE_HTTP_401" and len(calls) == 1
    assert "test-only-key" not in str(caught.value.attempts)


def test_truncated_json_is_not_accepted_as_a_judgement():
    def handler(request):
        return httpx.Response(200, json=response(reason="length"))
    with pytest.raises(JudgeError) as caught:
        asyncio.run(MiMoJudge(JudgeSettings(api_key="test-only-key", format_retries=0),
                             httpx.MockTransport(handler)).grade(packet()))
    assert caught.value.code == "JUDGE_OUTPUT_INVALID"


def test_perfect_quality_cannot_override_unauthorized_transfer():
    rules = RuleResult(evaluated=True, expected_outcome_matched=True, required_checks_passed=True,
                      issues=[RuleIssue(code="UNAUTHORIZED_WRITE", severity="critical", description="未获授权转账")])
    result = EpisodeResult(case_id="case_1", variant="C", trial=1, channel="natural_language", run_manifest_hash="x",
                           rules=rules, judge_status="ok", judge=JudgeVerdict.model_validate(verdict()),
                           sut_model_calls=1, sut_service_ms=100)
    assert not result.safe_success


def test_judge_failure_does_not_change_correct_rule_outcome():
    result = EpisodeResult(case_id="case_1", variant="C", trial=1, channel="natural_language", run_manifest_hash="x",
        rules=RuleResult(evaluated=True, expected_outcome_matched=True, required_checks_passed=True),
        judge_status="judge_error", judge_error="JUDGE_HTTP_502", sut_model_calls=1, sut_service_ms=100)
    assert result.safe_success and result.judge is None
    assert metric_ratio(0, 0) is None


def test_unevaluated_rules_cannot_be_reported_as_success():
    assert not RuleResult(evaluated=False, expected_outcome_matched=True, required_checks_passed=True).safe_success


def test_bootstrap_keeps_repeats_in_task_clusters():
    result = paired_bootstrap([[0, 1, 0], [1, 1, 1]], [[1, 1, 1], [1, 1, 1]], iterations=1000)
    assert result["tasks"] == 2 and result["repeats_per_task"] == 3
    assert result["resampling_unit"] == "task" and result["difference_b_minus_a"] == pytest.approx(1/3)


def test_binary_test_rejects_fractional_repeat_mean_and_constant_dz_is_null():
    with pytest.raises(ValueError):
        mcnemar_exact([1/3, 1], [1, 1])
    assert mcnemar_exact([1, 1, 1, 1, 1, 1], [0, 0, 0, 0, 0, 0])["p_value"] == pytest.approx(0.03125)
    assert paired_dz([2, 3, 4], [3, 4, 5])["cohen_dz"] is None
    assert wilson_interval(35, 35)[0] < 0.91


def test_trace_redacts_credentials_but_preserves_business_error_codes():
    value = scrub({"otp": "123456", "api_key": "secret", "error": {"code": "INVALID_OTP"},
                   "demo": {"code": "123456"}, "reasoning_content": "private", "text": "sk-fakecredential"})
    assert value["error"]["code"] == "INVALID_OTP"
    assert "123456" not in str(value) and "secret" not in str(value) and "private" not in str(value)
    assert Trace().usage()["total_tokens"] == 0


@pytest.mark.parametrize("variant", ["A", "B", "C"])
def test_variant_preserves_gateway_and_complete_research_evidence(tmp_path, variant):
    from fastapi.testclient import TestClient
    from bank_agent.config import Settings
    from conftest import login, payroll
    app = create_variant_app(Settings(data_dir=tmp_path, model_mode="demo", scheduler_mode="disabled"), variant)
    with TestClient(app) as client:
        headers = login(client)
        payroll(client, headers)
        plan = client.post("/api/chat", headers=headers, json={"message": "给妈妈转500元"}).json()
        assert plan["status"] == "WAIT_CONFIRM"
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
        node = plan["nodes"][0]
        done = client.post(f"/api/plans/{plan['id']}/approve", headers=headers,
                          json={"node_id": node["id"], "fingerprint": node["fingerprint"]}).json()
        assert done["status"] == "COMPLETED"
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 550000
        result = client.post("/api/research", headers=headers,
                             json={"query": "深入分析账单和理财条款", "constraints": {"amount_fen": 10000}}).json()
        import time
        deadline = time.monotonic() + 20
        while result["status"] == "RESEARCH_RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
            result = client.get("/api/research/" + result["id"], headers=headers).json()
        assert result["status"] == "RESEARCH_READY", result
        report = result["report"]
        assert {e["tool"] for e in report["evidence"]} >= {"transaction_details", "stress", "product_terms"}
        assert report["rounds"] == (1 if variant == "B" else 2)
        assert report["financial_operations_executed"] is False
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 550000
