from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.storage import canonical
from bank_agent.token_budget import RequestTokenBudget, TaskTokenBudget


def request(payload, system="bank instructions", tools=None):
    return {"model": "synthetic", "messages": [{"role": "system", "content": system},
            {"role": "user", "content": canonical(payload)}], "max_tokens": 256,
            "tools": tools or [], "response_format": {"type": "json_object"}}


def budget(tmp_path, limit=131072):
    return RequestTokenBudget(replace(Settings(data_dir=tmp_path), model_context_tokens=limit, model_token_safety_margin=64))


def test_complete_serialized_prompt_includes_system_schema_history_results_and_output_reserve(tmp_path):
    body = request({"message": "转500元，不要动用预留。", "context": {"turns": [{"role": "user", "content": "原始要求。"}]},
                    "facts": {"balance_fen": 100000}, "evidence": {"tool_result": "完整证据。" * 50}},
                   system="bank rules " * 70,
                   tools=[{"type": "function", "function": {"name": "proposal", "parameters": {"description": "schema " * 90}}}])
    _, accounting = budget(tmp_path).prepare(body)
    assert accounting["estimated_input_tokens"] > len(canonical(body).encode("utf-8"))
    assert accounting["output_reserved"] == 256 and accounting["safety_margin"] == 64
    assert accounting["counter"] == "utf8_conservative_estimate"
    with pytest.raises(BankError) as caught:
        budget(tmp_path, accounting["estimated_input_tokens"] + 256 + 64 - 1).prepare(body)
    assert caught.value.code == "MODEL_CONTEXT_BUDGET_EXCEEDED"


def test_optional_background_is_evicted_without_altering_pinned_data_or_caller_payload(tmp_path):
    protected = {"negative_constraints": ["不要动用生日预算。"], "fields": [{"name": "amount_fen", "value": 50000}]}
    payload = {"message": "给妈妈转500元，不要动用生日预算。", "facts": {"balance_fen": 100000},
               "context": {"shared_memory": {"protected": protected, "reread_required": ["evidence_original"],
                                               "items": []}}}
    base, stats = budget(tmp_path).prepare(request(payload))
    limit = stats["estimated_input_tokens"] + 256 + 64 + 10
    payload["context"]["shared_memory"]["items"] = [{"id": "optional", "kind": "dialogue", "text": "通用背景介绍。" * 500}]
    original = canonical(payload)
    prepared, stats = budget(tmp_path, limit).prepare(request(payload))
    result = json.loads(prepared["messages"][-1]["content"])
    assert stats["removed_items"] == 1 and stats["estimated_input_tokens"] + 256 + 64 <= limit
    assert result["context"]["shared_memory"]["protected"] == protected
    assert result["context"]["shared_memory"]["reread_required"] == ["evidence_original"]
    assert result["message"] == payload["message"] and result["facts"] == payload["facts"]
    assert prepared["tools"] == base["tools"] and canonical(payload) == original


@pytest.mark.parametrize("item", [
    {"kind": "dialogue", "text": "旧预算500元。" * 100},
    {"kind": "preference", "text": "报告偏好。" * 100},
    {"kind": "analysis", "text": "说明。" * 100, "critical_originals": ["不得赎回。"]},
    {"kind": "analysis", "text": "说明。" * 100, "protected_fields": [{"name": "amount_fen", "value": 100}]},
])
def test_mandatory_memories_never_disappear_to_satisfy_budget(tmp_path, item):
    with pytest.raises(BankError):
        budget(tmp_path, 1100).prepare(request({"message": "按原要求办理。", "shared_memory": {"items": [item]}}))


def test_only_noncritical_older_turns_are_removed_and_recent_pair_is_preserved(tmp_path):
    pair = [{"role": "user", "content": "最近的请求。"}, {"role": "assistant", "content": "最近的回复。"}]
    payload = {"message": "继续核对。", "context": {"active_plan": {"id": "plan_one"}, "turns": pair}}
    _, stats = budget(tmp_path).prepare(request(payload))
    limit = stats["estimated_input_tokens"] + 256 + 64 + 10
    payload["context"]["turns"] = [{"role": "assistant", "content": "背景介绍。" * 1000}, *pair]
    result, stats = budget(tmp_path, limit).prepare(request(payload))
    assert stats["removed_turns"] == 1
    assert json.loads(result["messages"][-1]["content"])["context"]["turns"] == pair


def test_large_user_request_tool_results_and_schemas_are_never_silently_truncated(tmp_path):
    for body in [request({"message": "用户原始请求。" * 500}), request({"evidence": {"text": "银行证据。" * 500}}),
                 request({}, tools=[{"parameters": {"description": "工具Schema。" * 500}}])]:
        with pytest.raises(BankError):
            budget(tmp_path, 2000).prepare(body)


def test_parallel_roles_reserve_from_one_shared_task_budget_atomically(client):
    limiter = TaskTokenBudget(replace(client.app.state.settings, task_token_budget=1500))
    db = client.app.state.db

    def reserve(index):
        record = {"id": f"run_reserve_{index}", "owner_id": "u_demo", "task_id": "task_parallel", "role": "business"}
        try:
            with db.write() as session:
                limiter.reserve(session, record, {"estimated_input_tokens": 600, "output_reserved": 400})
            return record
        except BankError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reserve, [1, 2]))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert "TASK_TOKEN_BUDGET_EXCEEDED" in results


@pytest.mark.parametrize("reported,started,charged", [(None, True, 1000), (None, False, 0), (100, True, 100)])
def test_task_accounting_releases_only_known_unused_or_never_dispatched_tokens(client, reported, started, charged):
    from bank_agent.storage import Document

    limiter, db = TaskTokenBudget(replace(client.app.state.settings, task_token_budget=1500)), client.app.state.db
    record = {"id": "run_settle", "owner_id": "u_demo", "task_id": "task_settle", "role": "business"}
    with db.write() as session:
        limiter.reserve(session, record, {"estimated_input_tokens": 600, "output_reserved": 400})
    record["_provider_started"] = started
    with db.write() as session:
        limiter.settle(session, record, reported)
        limiter.settle(session, record, reported)  # Replayed completion cannot double-release.
    with db.read() as session:
        ledger = session.get(Document, record["_budget_key"]).data
        assert ledger["allocated"] == charged
        assert ledger["charges"][record["id"]]["settled"]
