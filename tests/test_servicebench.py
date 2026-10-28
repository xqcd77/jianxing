"""Counterexamples to score inflation, data leakage and circular success checks."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from evaluation.config import JudgeSettings
from evaluation.servicebench.calibration import probes
from evaluation.servicebench.catalog import scenarios
from evaluation.servicebench.dataset import audit, export
from evaluation.servicebench.judge import (ServiceJudge, Verdict, candidate_quote_spans,
    normalize_tool_verdict, strict_verdict_tool)
from evaluation.servicebench.packets import api_text, build
from evaluation.servicebench.runner import ServiceEpisode, freeze
from evaluation.servicebench.scoring import assess, summarize


def scenario(key="service_identity"):
    return next(s for s in scenarios() if s.execution.id == key)


def sample_episode():
    return {"trial": 1, "sut_error": None, "state": {"balance_fen": 600000},
        "rules": {"safe_success": True, "policy_failures": []}, "observations": {},
        "responses": [{"label": "turn_1", "user_text": "你是谁", "response": {"message": "真实系统答复"}}]}


def verdict(packet, failed=None, unknown=None):
    finding = [{"criterion_id": r["id"], "verdict": "not_met" if r["id"] == failed else
        "insufficient_evidence" if r["id"] == unknown else "met", "candidate_quote": "真实系统答复",
        "evidence_ids": ["oracle"], "reason": "这是一条用于校验协议的测试理由"} for r in packet.requirements]
    return {"findings": finding, "scores": {d: {"score": (3 if failed else 5) if d in packet.applicable_dimensions else None, "reason": "用于核查分数边界的测试理由"}
        for d in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")},
        "failure_modes": [], "confidence": "high", "summary": "此处只测试评测协议，不代表实际裁判质量"}


def test_suite_balance_and_no_form_shortcuts(tmp_path):
    result = export(tmp_path)
    assert result["cases"] == 64 <= 70
    assert result["difficulty"] == {"simple": 14, "medium": 27, "complex": 23}
    assert len(result["domains"]) == 10 and result["semantic_requirements"] == 257
    assert result["measured_business_forms"] == 0 and result["human_labels"] == 0
    assert result["provenance"]["observed_failure_regression"] == 10
    public = [json.loads(line) for line in (tmp_path/"public.jsonl").read_text(encoding="utf-8").splitlines()]
    for item in public:
        assert not {"oracle", "checks", "requirements", "expected_behavior"} & set(item)
        assert all(turn["kind"] != "business" for turn in item["turns"])
    assert audit()["dataset_hash"] == result["dataset_hash"]


def test_gold_needs_task_specific_semantics_and_real_execution():
    for s in scenarios():
        assert len(s.requirements) >= 4 and all(r.required for r in s.requirements)
        assert s.execution.checks and s.oracle and s.preconditions
        if s.execution.allowed_writes:
            assert any(c.path not in {"ledger_sum", "account_sum", "mutating_receipt_count"} for c in s.execution.checks)


def test_answer_channel_excludes_user_gold_and_raw_backend_success():
    s, episode = scenario(), sample_episode()
    episode["responses"][0]["user_text"] = "USER_TEXT_ONLY"
    episode["responses"][0]["response"]["nodes"] = [{"result": {"data": {"proof": "RAW_BACKEND_ONLY"}}}]
    packet = build(s, episode)
    assert "真实系统答复" in packet.candidate_output
    assert "USER_TEXT_ONLY" not in packet.candidate_output
    assert "RAW_BACKEND_ONLY" not in packet.candidate_output
    assert s.oracle not in packet.candidate_output


@pytest.mark.parametrize("text", ["", "业务需求", "   ", "正在处理"])
def test_empty_answer_cannot_pass_even_with_all_bank_assertions_and_judge_met(text):
    s, episode = scenario(), sample_episode()
    packet = build(s, episode)
    judged = {"status": "ok", "verdict": verdict(packet)}
    result = assess(s, episode, judged, text)
    assert result["state_assertions_pass"]
    assert result["status"] == "failed" and not result["goal_fulfilled"]
    assert "no_answer" in result["failure_modes"]


def test_critical_financial_failure_cannot_be_averaged_away():
    s, episode = scenario(), sample_episode()
    episode["rules"]["policy_failures"] = ["未经授权发生资金变动"]
    episode["rules"]["safe_success"] = False
    packet = build(s, episode)
    result = assess(s, episode, {"status": "ok", "verdict": verdict(packet)}, packet.candidate_output)
    assert result["status"] == "failed"
    assert result["invariant_safe"] is False


def test_inconsistent_success_flag_cannot_override_a_recorded_safety_violation():
    s, episode = scenario(), sample_episode()
    episode["rules"]["policy_failures"] = ["未经授权发生资金变动"]
    packet = build(s, episode)
    result = assess(s, episode, {"status": "ok", "verdict": verdict(packet)}, packet.candidate_output)
    assert result["status"] == "failed" and result["invariant_safe"] is False


def test_over_refusal_fails_semantics_despite_safe_state():
    s, episode = scenario("subscriptions_cancel"), sample_episode()
    packet = build(s, episode)
    result = assess(s, episode, {"status": "ok", "verdict": verdict(packet, failed="r1")}, packet.candidate_output)
    assert result["status"] == "failed" and result["semantic_credit"] < 1


def test_insufficient_evidence_stays_unresolved_not_passed():
    s, episode = scenario(), sample_episode()
    packet = build(s, episode)
    result = assess(s, episode, {"status": "ok", "verdict": verdict(packet, unknown="r1")}, packet.candidate_output)
    assert result["status"] == "unresolved" and result["handled_as_expected"] is None


@pytest.mark.parametrize("error_status", ["judge_error", "packet_error"])
@pytest.mark.parametrize("hard_checks_pass", [True, False])
def test_finished_judge_error_is_unresolved_without_masking_objective_failure(error_status, hard_checks_pass):
    s, episode = scenario(), sample_episode()
    episode["rules"]["safe_success"] = hard_checks_pass
    result = assess(s, episode, {"status": error_status}, api_text(episode))
    assert result["status"] == ("unresolved" if hard_checks_pass else "failed")
    assert result["handled_as_expected"] is (None if hard_checks_pass else False)


def test_safe_capability_gap_never_counts_as_completed_business():
    s, episode = scenario("service_loan_gap"), sample_episode()
    packet = build(s, episode)
    result = assess(s, episode, {"status": "ok", "verdict": verdict(packet)}, packet.candidate_output)
    assert result["status"] == "passed" and not result["goal_fulfilled"]
    report = summarize([result], 1)
    assert report["business_completion"] == {"requested": 1, "fulfilled": 0}


@pytest.mark.parametrize("kind", ["wrong_yield", "wrong_currency", "wrong_date", "outage"])
def test_fluent_all_met_cannot_override_typed_market_fact_checks(kind):
    s = scenario("public_source_outage" if kind == "outage" else "public_rate")
    episode = sample_episode()
    market = {"currency": "CNY", "tenor": "10年", "yield_pct": "1.80", "data_date": "2026-10-12"}
    if kind == "wrong_yield":
        market["yield_pct"] = "9.99"
    elif kind == "wrong_currency":
        market["currency"] = "USD"
    elif kind == "wrong_date":
        market["data_date"] = "2026-10-16"
    episode["observations"] = {"turn_1": {"market_data": market}}
    packet = build(s, episode)
    row = assess(s, episode, {"status": "ok", "verdict": verdict(packet)}, packet.candidate_output)
    assert row["status"] == "failed" and row["failure_modes"]


def test_unjudged_and_unrun_tasks_remain_in_denominator():
    s, episode = scenario(), sample_episode()
    row = assess(s, episode, candidate=api_text(episode))
    result = summarize([row], 4)["expected_handling"]
    assert result["expected"] == 4 and result["passed"] == 0
    assert result["pending_or_unresolved"] == 4
    assert result["pass_rate_lower_bound"] == 0 and result["pass_rate_upper_bound"] == 1


@pytest.mark.parametrize('status',[401,402,403])
def test_provider_account_error_stops_queued_judge_calls(status):
    from evaluation.llm_judge import JudgeError
    calls=[]
    def provider(request):
        calls.append(1)
        return httpx.Response(status,json={'error':{'code':'synthetic'}})
    async def run():
        judge=ServiceJudge(JudgeSettings(api_key='synthetic',concurrency=1),httpx.MockTransport(provider))
        async def immediate():
            pass
        judge._rate_slot=immediate
        packet=build(scenario(),sample_episode())
        return await asyncio.gather(*(judge.grade(packet) for _ in range(5)),return_exceptions=True)
    errors=asyncio.run(run())
    assert len(calls)==1 and all(isinstance(error,JudgeError) for error in errors)
    assert errors[0].code==f'JUDGE_HTTP_{status}'
    assert all(error.code==f'JUDGE_SCHEDULER_STOPPED_JUDGE_HTTP_{status}' for error in errors[1:])


@pytest.mark.parametrize('original_status,original_code',[('judge_error','JUDGE_HTTP_402'),('ok',None),('judge_error','JUDGE_OUTPUT_INVALID')])
def test_infrastructure_recovery_preserves_prior_result_and_refuses_quality_regrading(tmp_path,original_status,original_code):
    import hashlib
    from evaluation.servicebench.workflow import load_judgement
    episode=tmp_path/'episode.json'
    prior={'status':original_status,'code':original_code,'protocol':{'scope':'frozen'},'packet_hash':'packet','episode_hash':'episode','attempts':[{'status':'http_error'}]}
    original=episode.with_name('judge_dom_v4.json')
    original.write_text(json.dumps(prior),encoding='utf8')
    original_bytes=original.read_bytes()
    recovery={**prior,'status':'ok','attempts':[{'status':'ok'}],'recovery_of_sha256':hashlib.sha256(original_bytes).hexdigest()}
    episode.with_name('judge_dom_v4_recovery.json').write_text(json.dumps(recovery),encoding='utf8')
    if original_status=='judge_error' and original_code in {'JUDGE_HTTP_402','JUDGE_OUTPUT_INVALID'}:
        selected=load_judgement(episode,'dom_v4')
        assert selected['status']=='ok' and len(selected['attempts'])==2
        if original_code=='JUDGE_OUTPUT_INVALID':
            assert selected['recovery_kind']=='invalid_output'
    else:
        with pytest.raises(ValueError):
            load_judgement(episode,'dom_v4')
    assert original.read_bytes()==original_bytes


def test_persisted_invalid_format_recovery_keeps_both_attempt_records(tmp_path):
    import hashlib
    from evaluation.servicebench.workflow import load_judgement
    episode=tmp_path/"episode.json"
    prior={"status":"judge_error","code":"JUDGE_OUTPUT_INVALID","protocol":{"scope":"v8"},
        "packet_hash":"packet","episode_hash":"episode","attempts":[{"status":"format_error"}]}
    original=episode.with_name("judge_dom_v8.json")
    original.write_text(json.dumps(prior),encoding="utf8")
    recovered={**prior,"status":"ok","attempts":[{"status":"ok"}],
        "recovery_of_sha256":hashlib.sha256(original.read_bytes()).hexdigest()}
    episode.with_name("judge_dom_v8_recovery.json").write_text(json.dumps(recovered),encoding="utf8")
    selected=load_judgement(episode,"dom_v8")
    assert selected["status"]=="ok" and selected["recovery_kind"]=="invalid_output"
    assert len(selected["attempts"])==2 and not selected["recovered_infrastructure"]


@pytest.mark.parametrize("damage", ["invent_quote", "no_quote", "omit_criterion", "invent_source", "contradict_score", "omit_evidence_dimension"])
def test_judge_output_validation_catches_optimistic_or_fabricated_claims(damage):
    s = scenario("public_rate")
    packet = build(s, sample_episode())
    output = verdict(packet)
    if damage == "invent_quote":
        output["findings"][0]["candidate_quote"] = "标准答案中有，但系统并没有说过"
    elif damage == "no_quote":
        output["findings"][0]["candidate_quote"] = ""
    elif damage == "omit_criterion":
        output["findings"].pop()
    elif damage == "invent_source":
        output["findings"][0]["evidence_ids"] = ["NONEXISTENT"]
    elif damage == "contradict_score":
        output["findings"][0]["verdict"] = "not_met"
    else:
        output["scores"]["evidence_reasoning"]["score"] = None
    with pytest.raises(ValueError):
        Verdict.model_validate(output).validate_against(packet)


def test_mimo_v2_protocol_redacts_secrets_and_does_not_reveal_reasoning():
    packet = build(scenario(), sample_episode())
    output = verdict(packet)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "逐项finding" in body["messages"][0]["content"]
        assert "candidate_quote" in body["messages"][0]["content"]
        return httpx.Response(200, json={"model": "mimo-fixture", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(output, ensure_ascii=False), "reasoning_content": "PRIVATE_REASONING"}}]})

    judge = ServiceJudge(JudgeSettings(api_key="test-only", rpm=100), transport=httpx.MockTransport(respond))
    result = asyncio.run(judge.grade(packet))
    assert result["status"] == "ok" and len(requests) == 1
    assert "PRIVATE_REASONING" not in json.dumps(result)


def test_contrast_calibration_is_distinct_from_task_and_human_evaluation():
    items = probes()
    assert len(items) == 12
    assert len({x["pair"] for x in items}) == 6
    assert all(x["provenance"] == "author_assigned_contrast_not_human_reviewed" for x in items)
    assert any("49.99" in item["packet"].candidate_output for item in items)


def test_calibration_workflow_persists_inputs_and_individual_results(tmp_path, monkeypatch):
    from evaluation.servicebench import calibration

    class FakeJudge:
        def __init__(self, settings):
            pass

        async def grade(self, packet):
            return {"status": "ok", "verdict": {"findings": [
                {"criterion_id": r["id"], "verdict": "met"} for r in packet.requirements]}, "attempts": []}

    monkeypatch.setattr(calibration, "ServiceJudge", FakeJudge)
    result = asyncio.run(calibration.run_calibration(tmp_path, ["model_identity_good"]))
    assert result["total"] == result["matched"] == 1
    assert (tmp_path/"calibration_items/model_identity_good_packet.json").exists()
    assert (tmp_path/"calibration_items/model_identity_good_result.json").exists()
    assert result["human_agreement"] is None


def test_checkpoint_with_nodes_is_not_adopted_as_a_plan():
    from evaluation.runner import Episode
    from evaluation.schemas import Step
    episode = Episode.__new__(Episode)
    episode.plan, episode.research, episode.conversation_id = {"id": "original_plan", "nodes": []}, None, None
    episode.observations, episode.setup_mode = {}, True
    episode.capture(Step(kind="checkpoint", label="state"), {"nodes": [], "balance_fen": 600000})
    assert episode.plan["id"] == "original_plan"


def test_service_dimensions_do_not_give_free_financial_evidence_points():
    packet = build(scenario(), sample_episode())
    assert set(packet.applicable_dimensions) == {"coverage", "actionability", "clarity"}
    output = verdict(packet)
    assert Verdict.model_validate(output).validate_against(packet)
    output["scores"]["evidence_reasoning"]["score"] = 5
    with pytest.raises(ValueError):
        Verdict.model_validate(output).validate_against(packet)


def test_judge_schema_binds_exact_criterion_ids_for_each_packet():
    from evaluation.servicebench.judge import system_prompt
    packet = build(scenario(), sample_episode())
    prompt = system_prompt(packet)
    assert '"enum": ["r1", "r2", "r3", "r4"]' in prompt
    assert '"minItems": 4' in prompt and '"maxItems": 4' in prompt


def test_compact_judge_v5_binds_ids_and_applicable_dimensions_without_full_schema():
    from evaluation.servicebench.judge import system_prompt
    packet = build(scenario(), sample_episode())
    prompt = system_prompt(packet, revision='v5')
    assert '"r1", "r2", "r3", "r4"' in prompt
    assert 'applicable_dimensions' in prompt
    assert 'scores内部只能有上述五个维度' in prompt
    assert 'wrong_financial_fact' in prompt and 'confidence只能为high、medium或low' in prompt
    assert 'reason至少5字' in prompt and 'summary为5至40字' in prompt
    assert 'JSON Schema：' not in prompt
    assert len(prompt) < len(system_prompt(packet, revision='v4'))


def test_v6_strict_tool_schema_binds_all_fields_sources_and_quotes():
    packet = build(scenario("public_rate"), sample_episode())
    tool = strict_verdict_tool(packet, "v6")
    function = tool["function"]
    schema = function["parameters"]
    assert function["strict"] is True and function["name"] == "submit_verdict"
    assert set(schema["required"]) == set(schema["properties"])
    assert set(schema["properties"]["findings"]["required"]) == {r["id"] for r in packet.requirements}
    assert schema["properties"]["findings"]["additionalProperties"] is False
    quote_ids = candidate_quote_spans(packet)
    assert quote_ids and all(q in json.dumps(schema) for q in quote_ids)
    assert all(ref.id in json.dumps(schema) for ref in packet.evidence)


def test_v7_array_schema_and_scores_follow_packet_applicability():
    packet = build(scenario(), sample_episode())
    schema = strict_verdict_tool(packet, "v7")["function"]["parameters"]
    findings = schema["properties"]["findings"]
    assert findings["type"] == "array"
    assert findings["items"]["properties"]["criterion_id"]["enum"] == [r["id"] for r in packet.requirements]
    score_properties = schema["properties"]["scores"]["properties"]
    for dimension in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"):
        expected = list(range(1, 6)) if dimension in packet.applicable_dimensions else [0]
        assert score_properties[dimension]["properties"]["score"]["enum"] == expected
    assert "findings必须用数组" in __import__("evaluation.servicebench.judge", fromlist=["system_prompt"]).system_prompt(packet,"v7")


def test_v8_allows_two_format_repairs_and_discards_only_non_scoring_extras(monkeypatch):
    import evaluation.servicebench.judge as judge_module
    packet = build(scenario(), sample_episode())
    quote_id = next(iter(candidate_quote_spans(packet)))
    findings = [{"criterion_id": r["id"], "verdict": "met", "candidate_quote_id": quote_id,
        "evidence_ids": ["oracle"], "reason": "候选答复满足本项要求"} for r in packet.requirements]
    scores = {d: {"score": 5 if d in packet.applicable_dimensions else 0,
        "reason": "候选答复和验收要求相符"} for d in
        ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")}
    score_json = json.dumps({**scores, "clarity_note": "忽略的非评分字段"}, ensure_ascii=False)
    good = {"findings": findings, "scores": "评分如下：\n```json\n"+score_json+"\n```",
        "failure_modes": [], "confidence": "high", "summary": "候选答复符合本题验收要求", "extra_note": "unused"}
    responses = [{"findings": [], "scores": {}}, {k: v for k, v in good.items() if k != "summary"}, good]
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        args = responses[len(calls)-1]
        return httpx.Response(200, json={"model": "mimo-fixture", "choices": [{"finish_reason": "tool_calls",
            "message": {"tool_calls": [{"type": "function", "function": {
                "name": "submit_verdict", "arguments": json.dumps(args, ensure_ascii=False)}}]}}]})

    async def immediate_sleep(_seconds):
        return None
    monkeypatch.setattr(judge_module.asyncio, "sleep", immediate_sleep)
    judge = ServiceJudge(JudgeSettings(api_key="test-only", thinking=False, format_retries=2, rpm=100), httpx.MockTransport(respond))
    judge.revision = "v8"
    result = asyncio.run(judge.grade(packet))
    assert result["status"] == "ok" and len(calls) == 3
    assert [a["status"] for a in result["attempts"]] == ["format_error", "format_error", "ok"]
    assert result["attempts"][-1]["ignored_extra_fields"]
    assert result["verdict"]["scores"]["evidence_reasoning"]["score"] is None


def test_nested_json_field_extraction_does_not_guess_unstructured_values():
    from evaluation.servicebench.judge import _decode_json_field
    assert _decode_json_field('前置说明 ```json\n{"coverage": 4}\n```')[0] == {"coverage": 4}
    assert _decode_json_field('not a JSON score summary')[0] is None


def test_v6_normalizes_quote_ids_and_zero_scores_then_runs_business_validation():
    packet = build(scenario(), sample_episode())
    quote_id, quote = next(iter(candidate_quote_spans(packet).items()))
    raw = {"findings": {r["id"]: {"verdict": "met", "candidate_quote_id": quote_id,
        "evidence_ids": ["oracle"], "reason": "候选原句直接回应了该项要求"} for r in packet.requirements},
        "scores": {d: {"score": 5 if d in packet.applicable_dimensions else 0,
            "reason": "候选内容与本项要求及证据一致"} for d in
            ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")},
        "failure_modes": [], "confidence": "high", "summary": "候选回答覆盖了本题要求"}
    normalized = normalize_tool_verdict(raw, packet)
    assert all(f.candidate_quote == quote for f in normalized.findings)
    assert normalized.scores.evidence_reasoning.score is None
    raw["scores"]["coverage"]["score"] = False
    with pytest.raises(ValueError):
        normalize_tool_verdict(raw, packet)


def test_v6_uses_one_strict_tool_and_recovers_once_from_invalid_tool_output(monkeypatch):
    import evaluation.servicebench.judge as judge_module
    packet = build(scenario(), sample_episode())
    quote_id = next(iter(candidate_quote_spans(packet)))
    good = {"findings": {r["id"]: {"verdict": "met", "candidate_quote_id": quote_id,
        "evidence_ids": ["oracle"], "reason": "候选回答满足此项业务要求"} for r in packet.requirements},
        "scores": {d: {"score": 5 if d in packet.applicable_dimensions else 0,
            "reason": "按候选回复和适用范围评分"} for d in
            ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")},
        "failure_modes": [], "confidence": "high", "summary": "候选回答符合本题的验收要求"}
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert "response_format" not in body and body["tool_choice"] == "auto"
        assert body["tools"][0]["function"]["strict"] is True
        assert "⟦Q001⟧" in body["messages"][1]["content"]
        arguments = {"unexpected": True} if len(calls) == 1 else good
        return httpx.Response(200, json={"model": "mimo-fixture", "choices": [{"finish_reason": "tool_calls",
            "message": {"tool_calls": [{"type": "function", "function": {
                "name": "submit_verdict", "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}]})

    async def immediate_sleep(_seconds):
        return None
    monkeypatch.setattr(judge_module.asyncio, "sleep", immediate_sleep)
    settings = JudgeSettings(api_key="test-only", thinking=False, format_retries=1, rpm=100)
    judge = ServiceJudge(settings, httpx.MockTransport(respond))
    judge.revision = "v6"
    result = asyncio.run(judge.grade(packet))
    assert result["status"] == "ok" and len(calls) == 2
    assert [a["status"] for a in result["attempts"]] == ["format_error", "ok"]
    assert result["tool_schema_hash"] and result["verdict"]["findings"][0]["candidate_quote"] in packet.candidate_output


def test_v6_retries_a_transient_read_timeout_once(monkeypatch):
    import evaluation.servicebench.judge as judge_module
    packet = build(scenario(), sample_episode())
    quote_id = next(iter(candidate_quote_spans(packet)))
    arguments = {"findings": {r["id"]: {"verdict": "met", "candidate_quote_id": quote_id,
        "evidence_ids": ["oracle"], "reason": "候选答复满足本项要求"} for r in packet.requirements},
        "scores": {d: {"score": 5 if d in packet.applicable_dimensions else 0,
            "reason": "候选答复和验收要求相符"} for d in
            ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")},
        "failure_modes": [], "confidence": "high", "summary": "候选答复符合本题验收要求"}
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json={"model": "mimo-fixture", "choices": [{"finish_reason": "tool_calls",
            "message": {"tool_calls": [{"type": "function", "function": {
                "name": "submit_verdict", "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}]})

    async def immediate_sleep(_seconds):
        return None
    monkeypatch.setattr(judge_module.asyncio, "sleep", immediate_sleep)
    judge = ServiceJudge(JudgeSettings(api_key="test-only", thinking=False, rpm=100), httpx.MockTransport(respond))
    judge.revision = "v6"
    result = asyncio.run(judge.grade(packet))
    assert result["status"] == "ok" and len(requests) == 2
    assert [a["status"] for a in result["attempts"]] == ["deadline_exceeded", "ok"]


def test_efficiency_retains_unknown_tokens_and_separates_actual_model_use():
    from evaluation.servicebench.diagnostics import efficiency
    result = efficiency([
        {"sut_wall_ms": 100, "model_call_count": 1, "external_model_call_count": 1,
         "llm_exercised": True, "usage": {"total_tokens": 42}, "memory_backend": "fastembed"},
        {"sut_wall_ms": 300, "model_call_count": None, "usage": None}], [])
    assert result["episode_wall_ms"]["p50"] == 200
    assert result["sut_tokens"]["total_tokens"] == {"known_sum": 42, "missing_records": 1, "complete_total": None}
    assert result["model_call_count_unknown_episodes"] == 1
    assert result["episodes_using_model"] == 1


def test_judge_scheduling_override_preserves_scoring_and_rpm():
    from evaluation.servicebench.workflow import same_judge_scoring_configuration
    reference = JudgeSettings().public()
    assert same_judge_scoring_configuration(reference, {**reference, 'concurrency': 6})
    assert not same_judge_scoring_configuration(reference, {**reference, 'rpm': 60})
    assert not same_judge_scoring_configuration(reference, {**reference, 'thinking': False})


def test_fsm_coverage_excludes_setup_and_does_not_invent_unseen_edges():
    from evaluation.servicebench.diagnostics import state_coverage
    episode = {"case_id": "case", "responses": [{"response": {"id": "measured", "nodes": [{"status": "WAIT_MFA"}]}}],
        "state": {"audit": [
            {"kind": "WORKFLOW_TRANSITION", "plan_id": "setup", "details": {"from": "BACKTRACKING", "to": "REPLANNING"}},
            {"kind": "WORKFLOW_TRANSITION", "plan_id": "measured", "details": {"from": "COMPILED", "to": "VERIFYING"}},
            {"kind": "WORKFLOW_TRANSITION", "plan_id": "measured", "details": {"from": "FINISHED", "to": "EXECUTING"}}]}}
    result = state_coverage([episode])
    assert "REPLANNING" in result["workflow_states"]["uncovered"]
    assert result["workflow_transitions"]["observed"] == 1
    assert len(result["workflow_transitions"]["illegal_events"]) == 1
    assert result["node_snapshot_states"]["observed"] == ["WAIT_MFA"]


@pytest.mark.parametrize('key,expected_refund', [('bills_month', 0), ('bills_refund', 10000)])
def test_statement_gold_matches_independently_summed_source_rows(key, expected_refund):
    from sqlalchemy import select
    from bank_agent.storage import Transaction
    s = scenario(key)
    root = Path('.data/evaluation')/('test_ledger_oracle_'+uuid4().hex)
    episode = ServiceEpisode(s, 'C', root/'sandbox', 'demo', 'lexical')
    try:
        episode.prepare()
        # No BillsService/report result is used to derive this independent gold.
        with episode.app.state.db.read() as session:
            rows = list(session.scalars(select(Transaction).where(
                Transaction.owner_id == 'u_demo', Transaction.posted_at.startswith('2026-09'))))
        gross = sum(t.amount_fen for t in rows if t.kind == 'PAYMENT')
        refunds = sum(t.amount_fen for t in rows if t.kind == 'REFUND')
        transfers = sum(t.amount_fen for t in rows if t.kind == 'TRANSFER')
        assert gross == 580000 and refunds == expected_refund and transfers == 20000
        checks = {c.path: c.expected for c in s.execution.checks}
        prefix = 'by_op.bills_report.0.data.'
        assert checks[prefix+'gross_consumption_fen'] == gross
        assert checks[prefix+'consumption_fen'] == gross-refunds
        assert checks[prefix+'transfer_fen'] == transfers
    finally:
        episode.close()


def test_frozen_run_refuses_changed_code_or_selection():
    root = Path(".data/evaluation")/("test_service_freeze_"+uuid4().hex)
    _, _, first = freeze(root, ["service_identity"], mode="demo")
    _, _, second = freeze(root, ["service_identity"], mode="demo")
    assert first["manifest_hash"] == second["manifest_hash"]
    with pytest.raises(ValueError):
        freeze(root, ["service_capabilities"], mode="demo")


@pytest.mark.parametrize("key", ["service_identity", "service_model", "public_rate_followup", "public_source_outage", "funds_small"])
def test_isolated_sandbox_and_source_capture_smoke(key):
    s = scenario(key)
    root = Path(".data/evaluation")/("test_service_episode_"+uuid4().hex)
    episode = ServiceEpisode(s, "C", root/"sandbox", "demo", "lexical")
    result = episode.run()
    assert result["sut_error"] is None
    # The Chinese numeral transfer is deliberately a genuine challenge. A
    # system failure is retained; a harness smoke must not demand that every
    # benchmark case pass and then weaken gold to satisfy that demand.
    if key != "funds_small":
        assert result["rules"]["safe_success"] is True
    assert result["mode"] == "demo" and result["external_model_call_count"] == 0
    if key == "public_source_outage":
        assert result["observations"]["turn_1"]["source_status"] == "unavailable"
        assert episode.public_requests
    packet = build(s, result)
    assert packet.candidate_output and "oracle" not in packet.candidate_output
    # Correct financial state with no semantic judge is still not a task pass.
    expected_status = "pending" if result["rules"]["safe_success"] else "failed"
    assert assess(s, result, candidate=packet.candidate_output)["status"] == expected_status
