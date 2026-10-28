from dataclasses import replace
import json

import httpx
import pytest

from bank_agent.agents import ModelAdapter
from bank_agent.dialogue import money_slot, read_context
from bank_agent.errors import BankError, ClarificationNeeded
from bank_agent.schemas import Intent
from bank_agent.storage import Document, Plan
from sqlalchemy import select
from conftest import finish, login, payroll


def ask(client, headers, text, conversation=None):
    response = client.post(
        "/api/chat",
        headers=headers,
        json={"message": text, **({"conversation_id": conversation} if conversation else {})},
    )
    assert response.status_code == 200, response.text
    return response.json()


def wrong_model(client, monkeypatch, kind="amount"):
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode="llm")
    calls = []

    def structured(role, payload, schema):
        calls.append(payload)
        return Intent(summary="需要信息", clarification="请补充信息", clarification_kind=kind)

    monkeypatch.setattr(planner.model, "structured", structured)
    return calls


@pytest.mark.parametrize(
    "text", ["给妈妈转100000元", "给妈妈转10万元", "给妈妈转十万元", "给妈妈转100,000元", "给妈妈转10万"]
)
def test_explicit_amount_is_not_lost_when_model_asks_for_it(client, headers, monkeypatch, text):
    payroll(client, headers, 20000000)
    wrong_model(client, monkeypatch)
    result = ask(client, headers, text)
    assert result["status"] == "WAIT_MFA"
    assert result["nodes"][0]["params"]["amount_fen"] == 10000000
    assert "100,000元" in result["assistant_message"] and "金额是多少" not in result["assistant_message"]


def test_known_recipient_survives_short_amount_answer(client, headers, monkeypatch):
    payroll(client, headers, 20000000)
    calls = wrong_model(client, monkeypatch, "business")
    first = ask(client, headers, "给妈妈转账")
    assert first["status"] == "NEEDS_CLARIFICATION" and "金额" in first["message"]
    answer = ask(client, headers, "100000元啊", first["conversation_id"])
    assert answer["status"] == "WAIT_MFA"
    assert answer["nodes"][0]["params"]["recipient_hint"] == "妈妈"
    assert answer["nodes"][0]["params"]["amount_fen"] == 10000000
    assert len(calls) == 1  # The pending draft is filled locally, not reinterpreted by the model.


def test_corrected_money_survives_later_recipient_answer(client, headers, monkeypatch):
    payroll(client, headers, 20000000)
    wrong_model(client, monkeypatch)
    first = ask(client, headers, "给她转100000元")
    assert "收款人" in first["message"]
    revised = ask(client, headers, "改成50000元", first["conversation_id"])
    assert revised["status"] == "NEEDS_CLARIFICATION" and "收款人" in revised["message"]
    completed_slots = ask(client, headers, "妈妈", first["conversation_id"])
    assert completed_slots["nodes"][0]["params"]["amount_fen"] == 5000000


def test_repeated_amount_reuses_pending_plan_without_calling_model_or_moving_money(client, headers, monkeypatch):
    payroll(client, headers, 20000000)
    calls = wrong_model(client, monkeypatch)
    first = ask(client, headers, "给妈妈转100000元")
    initial_calls = len(calls)
    repeated = ask(client, headers, "100000元啊", first["conversation_id"])
    assert repeated["id"] == first["id"] and len(calls) == initial_calls
    assert repeated["status"] == "WAIT_MFA"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 20000000
    with client.app.state.db.read() as session:
        assert len(list(session.scalars(select(Plan)))) == 1


def test_insufficient_balance_is_a_business_constraint_not_missing_amount(client, headers, monkeypatch):
    wrong_model(client, monkeypatch)
    first = ask(client, headers, "给妈妈转100000元")
    assert first["status"] == "FAILED"
    assert first["nodes"][0]["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert "100,000元" in first["assistant_message"] and "可用余额为0元" in first["assistant_message"]
    assert "尚未转出" in first["assistant_message"]
    repeated = ask(client, headers, "100000元啊", first["conversation_id"])
    assert repeated["id"] == first["id"]
    with client.app.state.db.read() as session:
        counter = session.get(Document, "circuit_u_demo")
        assert counter is None or counter.data["failures"] == 0


def test_amount_correction_edits_original_plan_and_invalidates_old_approval(client, headers):
    payroll(client, headers, 20000000)
    first = ask(client, headers, "给妈妈转100000元")
    revised = ask(client, headers, "改成100元", first["conversation_id"])
    assert revised["id"] == first["id"] and revised["version"] == first["version"] + 1
    assert revised["status"] == "WAIT_CONFIRM" and revised["nodes"][0]["params"]["amount_fen"] == 10000
    assert revised["nodes"][0]["fingerprint"] != first["nodes"][0]["fingerprint"]
    assert finish(client, headers, revised)["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 19990000


def test_previous_balance_failure_is_not_presented_as_current_after_funding(client, headers):
    first = ask(client, headers, "给妈妈转100000元")
    assert first["status"] == "FAILED"
    payroll(client, headers, 20000000)
    repeated = ask(client, headers, "100000元啊", first["conversation_id"])
    assert repeated["id"] == first["id"] and "当前可用余额已足够" in repeated["assistant_message"]
    assert "尚未执行" in repeated["assistant_message"]
    resumed = client.post("/api/plans/" + first["id"] + "/resume", headers=headers, json={}).json()
    assert resumed["status"] == "WAIT_MFA"


def test_completed_transfer_is_not_repeated_by_short_amount_answer(client, headers):
    payroll(client, headers)
    first = ask(client, headers, "给妈妈转100元")
    completed = finish(client, headers, first)
    repeated = ask(client, headers, "100元啊", first["conversation_id"])
    assert repeated["id"] == first["id"] and repeated["status"] == "COMPLETED"
    assert repeated["nodes"][0]["result"]["receipt_id"] == completed["nodes"][0]["result"]["receipt_id"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 590000


def test_dialogue_state_and_draft_are_scoped_and_new_business_drops_old_question(client, headers):
    first = ask(client, headers, "给她转100000元")
    assert first["status"] == "NEEDS_CLARIFICATION"
    cid = first["conversation_id"]
    other = ask(client, login(client, "wang"), "妈妈", cid)
    assert "nodes" not in other
    query = ask(client, headers, "查余额", cid)
    assert query["status"] == "COMPLETED"
    ctx = read_context(client.app.state.db, "u_demo", cid, "妈妈", client.app.state.bank.facts("u_demo"))
    assert "pending_draft" not in ctx
    assert len(ctx["turns"]) == 4 and ctx["active_plan"]["status"] == "COMPLETED"


def test_model_amount_unit_error_is_replaced_by_user_grounded_amount(client, headers, monkeypatch):
    payroll(client, headers, 20000000)
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode="llm")
    monkeypatch.setattr(
        planner.model,
        "structured",
        lambda *args: Intent(
            summary="转账",
            tasks=[
                {
                    "domain": "funds",
                    "action": "funds.transfer",
                    "params": {"recipient_hint": "妈妈", "amount_fen": 100000},
                }
            ],
        ),
    )
    result = ask(client, headers, "给妈妈转100000元")
    assert result["nodes"][0]["params"]["amount_fen"] == 10000000


@pytest.mark.parametrize("text", ["给张三转100元生活费", "给张妈妈转100元"])
def test_notes_or_partial_names_do_not_override_an_explicit_unknown_recipient(client, headers, text):
    payroll(client, headers)
    reply = ask(client, headers, text)
    assert reply["status"] == "NEEDS_CLARIFICATION" and "收款人" in reply["message"]
    assert "nodes" not in reply


@pytest.mark.parametrize("text", ["−100元", "-100元", "8.001元"])
def test_invalid_user_money_is_not_silently_normalized(text):
    with pytest.raises(ClarificationNeeded):
        money_slot(text)


def test_thinking_max_uses_compatible_protocol_and_discards_reasoning(monkeypatch, tmp_path):
    from bank_agent.config import Settings

    settings = Settings(data_dir=tmp_path, model_api_key="unit-test-only", model_thinking=True)
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                                "content": json.dumps(
                                    {"summary": "查询余额", "tasks": [{"domain": "funds", "action": "balance.query"}]}
                                ),
                            },
                        }
                    ]
                },
            )

    monkeypatch.setattr("bank_agent.agents.httpx.Client", Client)
    intent = ModelAdapter(settings).structured("planner", {"message": "查余额"}, Intent)
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["reasoning_effort"] == "max" and "tool_choice" not in requests[0] and "tools" not in requests[0]
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] == 16384 and "temperature" not in requests[0]
    assert "PRIVATE_REASONING_SENTINEL" not in intent.model_dump_json()
    prompt = requests[0]["messages"][0]["content"]
    assert "余额不足" in prompt and "100000元=10000000分" in prompt and "pending_draft" in prompt


def test_invalid_thinking_effort_fails_before_network_call(tmp_path):
    from bank_agent.config import Settings

    settings = Settings(
        data_dir=tmp_path, model_api_key="unit-test-only", model_thinking=True, model_reasoning_effort="unsupported"
    )
    with pytest.raises(BankError, match="推理强度"):
        ModelAdapter(settings).structured("planner", {}, Intent)
