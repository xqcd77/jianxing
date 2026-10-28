"""Opt-in research integration; reads the locally configured key without printing it."""
from dataclasses import replace
import os
import time

from fastapi.testclient import TestClient
import pytest

from bank_agent.app import create_app
from bank_agent.config import Settings
from conftest import login, payroll


@pytest.mark.live_model
@pytest.mark.skipif(os.getenv("BANK_TEST_LIVE_MODEL") != "1", reason="Set BANK_TEST_LIVE_MODEL=1 to call the model")
def test_deepseek_research_supervisor_and_parallel_roles(tmp_path, monkeypatch):
    settings = replace(Settings.from_env(), data_dir=tmp_path, model_mode="llm", memory_embedding="lexical")
    assert settings.model_thinking and settings.model_reasoning_effort == "max"
    with TestClient(create_app(settings)) as client:
        headers = login(client)
        payroll(client, headers, 2000000)
        service = client.app.state.research
        original = service.model.astructured
        calls = []

        async def capture(role, payload, schema):
            result = await original(role, payload, schema)
            calls.append(role)
            return result

        monkeypatch.setattr(service.model, "astructured", capture)
        response = client.post("/api/chat", headers=headers, json={"message":
            "深入研究我的账单和理财产品。计划申购5000元，生活费6000元，应急资金3000元，"
            "2026-11-20需要用款，解释疑似重复扣费以及产品排除原因。"})
        assert response.status_code == 200
        data = response.json()
        deadline = time.monotonic() + 360
        while data["status"] == "RESEARCH_RUNNING" and time.monotonic() < deadline:
            time.sleep(1)
            data = client.get("/api/research/" + data["id"], headers=headers).json()
        assert data["status"] == "RESEARCH_READY", data.get("message")
        r = data["report"]
        assert r["quality"] == "verified", r["quality_note"]
        assert r["constraints"]["amount_fen"] == 500000
        assert r["constraints"]["reserve_fen"] == 600000
        assert r["constraints"]["emergency_fen"] == 300000
        assert {"supervisor", "bills", "wealth"} <= set(calls)
        assert len(calls) <= 5 and r["rounds"] == 2
        assert any(o["id"] == "buy_P_CASH" for o in r["options"])
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 2000000
        assert not client.get("/api/plans", headers=headers).json()
