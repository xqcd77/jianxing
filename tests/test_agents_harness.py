import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import select

from bank_agent.agents import ModelAdapter
from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.harness import BankAgentHarness
from bank_agent.schemas import StrictModel
from bank_agent.storage import AgentRun, Database
from conftest import login


class Answer(StrictModel):
    outcome: str


def adapter(tmp_path, **overrides):
    settings = replace(Settings(data_dir=tmp_path, model_mode="llm", model_api_key="synthetic-test-key"), **overrides)
    model = ModelAdapter(settings)
    db = Database(tmp_path)
    model.harness = BankAgentHarness(settings, db)
    return model, db


def response(usage=None):
    data = {"choices": [{"finish_reason": "stop", "message": {
        "content": '{"outcome":"核对完成"}', "reasoning_content": "PRIVATE_REASONING_SENTINEL",
    }}]}
    if usage is not None:
        data["usage"] = usage
    return httpx.Response(200, json=data)


def test_sdk_runner_is_used_once_and_local_accounting_excludes_content(tmp_path, monkeypatch):
    from agents import Runner

    model, db = adapter(tmp_path)
    seen, calls = [], []
    original = Runner.run

    async def sdk_run(agent, **kwargs):
        seen.append((agent, kwargs))
        return await original(agent, **kwargs)

    def provider(request):
        calls.append(request)
        return response({"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150})

    monkeypatch.setattr(Runner, "run", sdk_run)
    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope("u_demo", "conversation_one", "task_one"):
        result = asyncio.run(model.astructured("planner", {"message": "PRIVATE_USER_INPUT"}, Answer))
    assert result.outcome == "核对完成" and len(calls) == len(seen) == 1
    agent, options = seen[0]
    assert agent.tools == [] and agent.handoffs == []
    assert options["max_turns"] == 1 and options["run_config"].tracing_disabled
    row = model.harness.list_runs("u_demo")[0]
    assert row["status"] == "SUCCEEDED" and row["runtime"] == "agents_sdk"
    assert row["conversation_id"] == "conversation_one" and row["task_id"] == "task_one"
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (120, 30, 150)
    assert row["duration_ms"] > 0 and row["finished_at"]
    assert "PRIVATE" not in str(row) and model.harness.list_runs("u_li") == []
    db.engine.dispose()


@pytest.mark.parametrize("runtime", ["agents_sdk", "legacy_http"])
def test_missing_usage_is_unknown_and_sync_adapter_still_works(tmp_path, runtime):
    model, db = adapter(tmp_path, agent_runtime=runtime)
    model.http_transport = httpx.MockTransport(lambda _: response())
    with model.harness.scope("u_demo"):
        assert model.structured("planner", {}, Answer).outcome == "核对完成"
    row = model.harness.list_runs("u_demo")[0]
    assert row["prompt_tokens"] is row["completion_tokens"] is row["total_tokens"] is None
    db.engine.dispose()


@pytest.mark.parametrize("failure,code", [
    (httpx.Response(401, json={"error": "PRIVATE_PROVIDER_ERROR"}), "MODEL_HTTP_ERROR"),
    (httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}), "MODEL_RESPONSE_INVALID"),
])
def test_sdk_preserves_provider_and_schema_failures_without_retries(tmp_path, failure, code):
    model, db = adapter(tmp_path)
    calls = []

    def provider(request):
        calls.append(request)
        return failure

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope("u_demo"), pytest.raises(BankError) as caught:
        asyncio.run(model.astructured("planner", {}, Answer))
    assert caught.value.code == code and len(calls) == 1
    if code == "MODEL_HTTP_ERROR":
        assert caught.value.provider_status == 401
    else:
        assert caught.value.diagnostic["kind"] == "schema_validation"
    row = model.harness.list_runs("u_demo")[0]
    assert row["status"] == "FAILED" and row["error_code"] == code
    assert "PRIVATE_PROVIDER_ERROR" not in str(row)
    db.engine.dispose()


@pytest.mark.parametrize("cancel", [False, True])
def test_deadline_and_cancellation_close_accounting_without_continuing(tmp_path, cancel):
    model, db = adapter(tmp_path, model_timeout=2 if cancel else 0.05)

    async def scenario():
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def provider(_):
            entered.set()
            try:
                await asyncio.sleep(20)
                raise AssertionError("cancelled call must never continue")
            finally:
                stopped.set()

        model.http_transport = httpx.MockTransport(provider)
        with model.harness.scope("u_demo"):
            task = asyncio.create_task(model.astructured("planner", {}, Answer))
            if cancel:
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(BankError) as caught:
                    await task
                assert caught.value.code == "MODEL_DEADLINE_EXCEEDED"
        assert stopped.is_set()

    asyncio.run(scenario())
    row = model.harness.list_runs("u_demo")[0]
    assert row["status"] == ("CANCELLED" if cancel else "TIMED_OUT")
    assert row["finished_at"]
    db.engine.dispose()


def test_scope_cannot_change_user_and_runtime_endpoint_is_owner_filtered(client, headers):
    harness = client.app.state.harness
    with harness.scope("u_demo"), pytest.raises(BankError) as caught:
        with harness.scope("u_li"):
            pass
    assert caught.value.code == "HARNESS_OWNER_MISMATCH"
    assert client.get("/api/runtime/runs").status_code == 401
    with client.app.state.db.write() as session:
        session.add(AgentRun(id="run_private", owner_id="u_demo", conversation_id="", task_id="one",
                             role="planner", schema_name="Answer", runtime="agents_sdk", model_name="mock",
                             status="SUCCEEDED", created_at="2026-10-23T00:00:00+00:00"))
    own = client.get("/api/runtime/runs", headers=headers).json()
    assert own["runtime"] == "agents_sdk" and own["items"][0]["id"] == "run_private"
    assert client.get("/api/runtime/runs", headers=login(client, "li")).json()["items"] == []
    with client.app.state.db.read() as session:
        assert len(list(session.scalars(select(AgentRun)))) == 1


def test_shared_task_cap_blocks_later_provider_calls_when_usage_is_unknown(tmp_path):
    model, db = adapter(tmp_path)
    body = model._request("planner", {"message": "synthetic"}, Answer)
    _, budget = model.harness.budget.prepare(body)
    settings = replace(model.settings, task_token_budget=budget["estimated_input_tokens"] + budget["output_reserved"])
    model.settings, model.harness = settings, BankAgentHarness(settings, db)
    calls = []

    def provider(request):
        calls.append(request)
        return response()

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope("u_demo", task_id="task_cap"):
        assert model.structured("planner", {"message": "synthetic"}, Answer).outcome == "核对完成"
        with pytest.raises(BankError) as caught:
            model.structured("planner", {"message": "synthetic"}, Answer)
    assert caught.value.code == "TASK_TOKEN_BUDGET_EXCEEDED" and len(calls) == 1
    assert {row["status"] for row in model.harness.list_runs("u_demo")} == {"SUCCEEDED", "FAILED"}
    db.engine.dispose()


def test_provider_receives_the_exact_budgeted_request_and_complete_user_input(tmp_path):
    model, db = adapter(tmp_path)
    payload = {"message": "转500元，不要动用预留。", "context": {"shared_memory": {"items": []}}}
    _, budget = model.harness.budget.prepare(model._request("planner", payload, Answer))
    limit = budget["estimated_input_tokens"] + budget["output_reserved"] + budget["safety_margin"] + 10
    settings = replace(model.settings, model_context_tokens=limit)
    model.settings, model.harness = settings, BankAgentHarness(settings, db)
    payload["context"]["shared_memory"]["items"] = [{"kind": "dialogue", "text": "通用背景介绍。" * 1000}]
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return response({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    model.http_transport = httpx.MockTransport(provider)
    with model.harness.scope("u_demo", task_id="task_trim"):
        model.structured("planner", payload, Answer)
    actual = json.loads(sent[0]["messages"][-1]["content"])
    assert actual["message"] == payload["message"] and actual["context"]["shared_memory"]["items"] == []
    assert len(payload["context"]["shared_memory"]["items"]) == 1
    events = model.harness.telemetry.events("u_demo", task_id="task_trim")["items"]
    checked = next(event["details"] for event in events if event["kind"] == "budget.checked")
    assert model.harness.budget.estimate(sent[0]) == checked["estimated_input_tokens"]
    assert checked["estimated_input_tokens"] + checked["output_reserved"] + checked["safety_margin"] <= limit
    db.engine.dispose()
