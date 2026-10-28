import asyncio
from dataclasses import replace

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from bank_agent.agents import ModelAdapter
from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.harness import BankAgentHarness
from bank_agent.runtime_middleware import RuntimeMiddleware
from bank_agent.schemas import Intent
from bank_agent.storage import AgentRun, audit
from conftest import login


def test_request_model_memory_and_business_events_share_correlation_and_metrics(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode="llm", model_api_key="synthetic-test-key"))

    def provider(_):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"summary":"查询余额","tasks":[{"domain":"funds","action":"balance.query","params":{}}]}',
            "reasoning_content": "PRIVATE_REASONING_SENTINEL",
        }}], "usage": {"prompt_tokens": 1200, "completion_tokens": 100, "total_tokens": 1300}})

    app.state.graph.planner.model.http_transport = httpx.MockTransport(provider)
    app.state.research.model.http_transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"path":"direct","reason":"synthetic"}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }))
    with TestClient(app) as client:
        headers = login(client)
        cursor = client.get("/api/runtime/events", headers=headers, params={"limit": 200}).json()["next_cursor"]
        response = client.post("/api/chat", headers=headers, json={"message": "我的账户还有多少钱？"})
        assert response.status_code == 200, response.text
        request_id = response.headers["x-request-id"]
        items = client.get("/api/runtime/events", headers=headers, params={"after": cursor, "limit": 200}).json()["items"]
        correlated = [item for item in items if item["request_id"] == request_id]
        kinds = {item["kind"] for item in correlated}
        assert {"request.started", "request.finished", "run.started", "run.finished", "budget.checked",
                "model.requested", "model.responded", "memory.searched", "workflow.transition"} <= kinds
        assert len({item["task_id"] for item in correlated}) == 1
        assert "PRIVATE_REASONING_SENTINEL" not in str(items)
        assert "synthetic-test-key" not in str(items) and "我的账户还有多少钱" not in str(items)
        data = client.get("/api/runtime/metrics", headers=headers).json()
        assert data["model_calls"]["provider_requests"] == 2
        assert data["model_calls"]["reported_total_tokens"] == 1420
        assert data["model_calls"]["usage_known_calls"] == 2
        assert data["model_calls"]["latency"]["count"] == 2
        assert "business" in data["model_calls"]["by_role"]
        assert data["http_requests"]["count"] >= 1
        assert data["budget"]["checked_calls"] == 2
        other = login(client, "li")
        assert client.get("/api/runtime/runs", headers=other).json()["items"] == []
        assert client.get("/api/runtime/metrics", headers=other).json()["model_calls"]["reported_total_tokens"] is None
        assert all(item["request_id"] != request_id for item in
                   client.get("/api/runtime/events", headers=other).json()["items"])
        for path in ["/api/runtime/events", "/api/runtime/metrics"]:
            assert client.get(path).status_code == 401
        assert client.get("/api/runtime/events", headers=headers, params={"limit": 201}).status_code == 422
        assert client.get("/api/runtime/metrics", headers=headers, params={"hours": 169}).status_code == 422


def test_budget_rejection_is_observable_and_never_reaches_provider(client):
    settings = replace(client.app.state.settings, model_mode="llm", model_api_key="synthetic-test-key",
                       model_context_tokens=3000, model_token_safety_margin=100)
    model = ModelAdapter(settings)
    model.harness = BankAgentHarness(settings, client.app.state.db)

    def forbidden(_):
        raise AssertionError("budget rejection must precede every network call")

    model.http_transport = httpx.MockTransport(forbidden)
    with model.harness.scope("u_demo", "conversation_budget", "task_budget"), pytest.raises(BankError) as caught:
        asyncio.run(model.astructured("planner", {"message": "保留原始金额500元，不要转账。"}, Intent))
    assert caught.value.code == "MODEL_CONTEXT_BUDGET_EXCEEDED"
    row = model.harness.list_runs("u_demo")[0]
    assert row["status"] == "FAILED" and row["total_tokens"] is None
    events = model.harness.telemetry.events("u_demo", limit=200, task_id="task_budget")["items"]
    assert [e["kind"] for e in events] == ["run.started", "budget.rejected", "run.finished"]
    assert events[1]["details"]["estimated_input_tokens"] + events[1]["details"]["output_reserved"] > 3000


def test_event_cursor_and_task_filter_do_not_leak_other_users_or_private_values(client):
    telemetry = client.app.state.harness.telemetry
    with client.app.state.harness.scope("u_demo", task_id="task_events"):
        telemetry.emit("synthetic.first", details={"status": "OK", "api_key": "PRIVATE_SECRET", "text": "PRIVATE_INPUT"})
        telemetry.emit("synthetic.other", "u_li", details={"status": "OK"})
        telemetry.emit("synthetic.last", details={"status": "OK"})
    first = telemetry.events("u_demo", limit=1, task_id="task_events")
    second = telemetry.events("u_demo", after=first["next_cursor"], task_id="task_events")
    assert first["items"][0]["kind"] == "synthetic.first"
    assert [e["kind"] for e in second["items"]] == ["synthetic.last"]
    assert "PRIVATE" not in str(first) + str(second)


def test_rolled_back_audit_does_not_publish_business_success(client):
    db = client.app.state.db
    with client.app.state.harness.scope("u_demo", task_id="task_rollback"):
        with pytest.raises(RuntimeError):
            with db.write() as session:
                audit(session, "u_demo", "SYNTHETIC_SUCCESS", {"operation": "funds.transfer"})
                raise RuntimeError("rollback")
    assert client.app.state.harness.telemetry.events("u_demo", task_id="task_rollback")["items"] == []


def test_http_latency_includes_streamed_body_and_cancelled_workflow_is_closed(client):
    telemetry = client.app.state.harness.telemetry
    app = FastAPI()
    app.add_middleware(RuntimeMiddleware, telemetry=telemetry)

    @app.get("/api/stream")
    def stream(request: Request):
        request.state.runtime_owner = "u_demo"

        async def body():
            yield "first"
            await asyncio.sleep(.06)
            yield "last"

        return StreamingResponse(body())

    with TestClient(app) as api:
        response = api.get("/api/stream")
        assert response.text == "firstlast"
    events = telemetry.events("u_demo", limit=200)["items"]
    finished = [e for e in events if e["request_id"] == response.headers["x-request-id"]]
    assert finished[-1]["details"]["duration_ms"] >= 50

    async def scenario():
        entered = asyncio.Event()

        async def job():
            with client.app.state.harness.scope("u_demo", task_id="task_cancelled_span"):
                async with telemetry.span("research", "u_demo"):
                    entered.set()
                    await asyncio.sleep(10)

        task = asyncio.create_task(job())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    events = telemetry.events("u_demo", task_id="task_cancelled_span")["items"]
    assert [e["kind"] for e in events] == ["workflow.started", "workflow.finished"]
    assert events[-1]["details"]["outcome"] == "cancelled"


def test_metrics_keep_unknown_usage_separate_from_zero_tokens_and_use_nearest_rank_p95(client):
    telemetry = client.app.state.harness.telemetry
    with client.app.state.db.write() as session:
        from bank_agent.storage import now_iso
        for index, duration in enumerate([10, 20, 30, 100]):
            session.add(AgentRun(id=f"synthetic_{index}", owner_id="u_demo", conversation_id="", task_id="",
                                 role="business", schema_name="Intent", runtime="agents_sdk", model_name="synthetic",
                                 status="SUCCEEDED", created_at=now_iso(), finished_at=now_iso(), duration_ms=duration,
                                 total_tokens=None if index == 3 else index * 100))
    data = telemetry.metrics("u_demo")["model_calls"]
    assert data["latency"] == {"count": 4, "avg_ms": 40, "p95_ms": 100}
    assert data["usage_known_calls"] == 3 and data["usage_unknown_calls"] == 1
    assert data["reported_total_tokens"] == 300 and data["avg_tokens_per_known_call"] == 100
    with client.app.state.db.read() as session:
        assert len(list(session.scalars(select(AgentRun)))) == 4
