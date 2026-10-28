"""Opt-in external integration; credentials are read locally, never emitted."""

from dataclasses import replace
import os

from fastapi.testclient import TestClient
import pytest

from bank_agent.app import create_app
from bank_agent.config import Settings
from conftest import login, payroll


@pytest.mark.live_model
@pytest.mark.skipif(os.getenv("BANK_TEST_LIVE_MODEL") != "1", reason="Set BANK_TEST_LIVE_MODEL=1 to call the model")
def test_deepseek_structured_intent_and_gateway(tmp_path):
    settings = replace(Settings.from_env(), data_dir=tmp_path, model_mode="llm", memory_embedding="lexical")
    assert settings.model_api_key, "Configure BANK_MODEL_API_KEY locally"
    with TestClient(create_app(settings)) as client:
        headers = login(client)
        payroll(client, headers)
        response = client.post("/api/chat", headers=headers, json={"message": "给妈妈转800元，分析2026年的年度账单"})
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["status"] == "WAIT_CONFIRM"
        node = next(n for n in plan["nodes"] if n["operation"] == "funds.transfer")
        assert node["params"]["amount_fen"] == 80000 and node["tier"] == "YELLOW"
        assert any(n["operation"] == "bills.report" for n in plan["nodes"])
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
        assert "funds.transfer" not in plan["assistant_message"] and "80000" not in plan["assistant_message"]
        question = client.post("/api/chat", headers=headers, json={"message": "给她转100元"}).json()
        assert question["status"] == "NEEDS_CLARIFICATION"
        assert "收款人" in question["message"] and "recipient" not in question["message"]
        product = client.post("/api/chat", headers=headers, json={"message": "申购500元理财"}).json()
        assert product["status"] == "NEEDS_CLARIFICATION"
        assert "产品" in product["message"] and "product_id" not in product["message"]
        incoming = client.post("/api/chat", headers=headers, json={"message": "现在我要向我的账户转入20000元"}).json()
        assert incoming["status"] == "WAIT_DEMO_CONFIRM" and incoming["ui_action"]["amount_fen"] == 2000000
        assert "2000000" not in incoming["message"] and "cashflow" not in incoming["message"]
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


@pytest.mark.live_model
@pytest.mark.skipif(os.getenv("BANK_TEST_LIVE_MODEL") != "1", reason="Set BANK_TEST_LIVE_MODEL=1 to call the model")
def test_deepseek_max_transfer_and_multiturn_context(tmp_path, monkeypatch):
    settings = replace(Settings.from_env(), data_dir=tmp_path, model_mode="llm", memory_embedding="lexical")
    assert settings.model_thinking and settings.model_reasoning_effort == "max"
    with TestClient(create_app(settings)) as client:
        headers = login(client)
        payroll(client, headers, 20000000)
        planner = client.app.state.graph.planner
        structured = planner.model.structured
        raw_plans = []

        def capture(*args):
            result = structured(*args)
            raw_plans.append(result)
            return result

        monkeypatch.setattr(planner.model, "structured", capture)
        first = client.post("/api/chat", headers=headers, json={"message": "给妈妈转100000元"}).json()
        assert first["status"] == "WAIT_MFA" and first["nodes"][0]["params"]["amount_fen"] == 10000000
        assert not raw_plans[0].clarification
        assert raw_plans[0].tasks[0].params["amount_fen"] == 10000000
        repeated = client.post(
            "/api/chat", headers=headers, json={"message": "100000元啊", "conversation_id": first["conversation_id"]}
        ).json()
        assert repeated["id"] == first["id"] and len(raw_plans) == 1
        unknown = client.post("/api/chat", headers=headers, json={"message": "给她转800元"}).json()
        assert unknown["status"] == "NEEDS_CLARIFICATION"
        answered = client.post(
            "/api/chat", headers=headers, json={"message": "妈妈", "conversation_id": unknown["conversation_id"]}
        ).json()
        assert "nodes" in answered, answered
        assert answered["nodes"][0]["params"]["recipient_hint"] == "妈妈"
        assert answered["nodes"][0]["params"]["amount_fen"] == 80000
        assert len(raw_plans) == 2  # The verified short recipient answer needs no new model call.
        assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 20000000
