"""Direction grounding and the failed transfer -> funding -> fresh approval flow."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from bank_agent.presentation import incoming_intent
from bank_agent.schemas import Intent
from bank_agent.storage import Receipt
from conftest import approve, login
from test_product_experience import ask, balance, decide


@pytest.mark.parametrize("text", ["向里面转账500元", "往里转500元", "往这个账户转500元"])
def test_relative_funding_uses_same_conversation_failed_transfer(client, headers, text, monkeypatch):
    blocked = ask(client, headers, "给妈妈转500元")
    assert blocked["status"] == "FAILED"
    assert "先向自己的账户转入资金" in blocked["assistant_message"]
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode="llm")

    def unexpected_model(*args):
        raise AssertionError("Grounded account funding must not fall through to the model tool catalog")

    monkeypatch.setattr(planner.model, "structured", unexpected_model)
    request = ask(client, headers, text, blocked["conversation_id"])
    assert request["status"] == "WAIT_DEMO_CONFIRM" and request["ui_action"]["amount_fen"] == 50000
    assert "给妈妈转账500元的安排会保留" in request["message"]
    assert balance(client, headers) == 0
    assert client.app.state.plans.snapshot("u_demo", blocked["id"])["status"] == "FAILED"


@pytest.mark.parametrize("text", ["往我的账户转500元", "给自己的卡转500元", "把五百元存入我的账户"])
def test_explicit_own_account_destinations_are_incoming_without_prior_context(client, headers, text):
    request = ask(client, headers, text)
    assert request["status"] == "WAIT_DEMO_CONFIRM" and request["ui_action"]["amount_fen"] == 50000
    assert balance(client, headers) == 0


def test_original_conversation_closes_without_automatic_transfer_or_duplicate_posting(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    cid = blocked["conversation_id"]
    request = ask(client, headers, "向里面转账500元", cid)
    posted = decide(client, headers, request)
    assert posted.status_code == 200, posted.text
    result = posted.json()
    assert result["continuation"]["plan_id"] == blocked["id"] and result["continuation"]["can_resume"]
    assert "尚未转出" in result["message"] and balance(client, headers) == 50000
    assert client.app.state.plans.snapshot("u_demo", blocked["id"])["status"] == "FAILED"
    resumed = client.post(f"/api/plans/{blocked['id']}/resume", headers=headers, json={}).json()
    assert resumed["id"] == blocked["id"] and resumed["status"] == "WAIT_CONFIRM"
    assert resumed["nodes"][0]["params"]["amount_fen"] == 50000 and balance(client, headers) == 50000
    completed = approve(client, headers, resumed)
    assert completed.status_code == 200 and completed.json()["status"] == "COMPLETED"
    assert balance(client, headers) == 0
    assert decide(client, headers, request).json() == result and balance(client, headers) == 0
    with client.app.state.db.read() as session:
        operations = [r.operation for r in session.scalars(select(Receipt))]
        assert operations.count("event.incoming") == operations.count("funds.transfer") == 1


def test_ambiguous_relative_destination_keeps_amount_until_owner_confirmation(client, headers):
    question = ask(client, headers, "向里面转账500元")
    assert question["status"] == "NEEDS_CLARIFICATION" and "自己的银行账户" in question["message"]
    assert "ui_action" not in question
    repeated = ask(client, headers, "500元", question["conversation_id"])
    assert repeated["status"] == "NEEDS_CLARIFICATION" and "ui_action" not in repeated
    answered = ask(client, headers, "是的", question["conversation_id"])
    assert answered["ui_action"]["amount_fen"] == 50000 and balance(client, headers) == 0


def test_relative_destination_never_uses_another_user_or_conversation(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    for user_headers, conversation in [(headers, None), (login(client, "wang"), blocked["conversation_id"])]:
        result = ask(client, user_headers, "向里面转账500元", conversation)
        assert result["status"] == "NEEDS_CLARIFICATION" and "ui_action" not in result
        assert blocked["id"] not in str(result)
    assert balance(client, headers) == 0


@pytest.mark.parametrize(
    "text",
    [
        "不要向里面转账500元",
        "向里面转账500元然后给妈妈转500元",
        "从我的账户向妈妈转账500元",
        "从我的账户转入妈妈的银行卡500元",
        "给妈妈转500元",
    ],
)
def test_outgoing_negative_and_mixed_requests_never_become_deposits(text):
    assert incoming_intent(text, own_account_context=True) is None


def test_partial_funding_does_not_offer_successful_transfer_or_ready_continuation(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    request = ask(client, headers, "向里面转账100元", blocked["conversation_id"])
    result = decide(client, headers, request).json()
    assert result["continuation"]["plan_id"] == blocked["id"] and not result["continuation"]["can_resume"]
    assert "仍受可用余额限制" in result["message"] and balance(client, headers) == 10000


def test_changed_plan_does_not_get_old_funding_continuation(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    request = ask(client, headers, "向里面转账500元", blocked["conversation_id"])
    cancelled = client.post(f"/api/plans/{blocked['id']}/cancel", headers=headers, json={})
    assert cancelled.status_code == 200
    result = decide(client, headers, request).json()
    assert "continuation" not in result and balance(client, headers) == 50000


def test_funding_dialogue_is_visible_to_next_planning_step_without_losing_original_plan(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    cid = blocked["conversation_id"]
    request = ask(client, headers, "向里面转账500元", cid)
    from bank_agent.dialogue import read_context

    context = read_context(client.app.state.db, "u_demo", cid, "继续", client.app.state.bank.facts("u_demo"))
    assert context["active_plan"]["id"] == blocked["id"]
    assert context["turns"][-2]["content"] == "向里面转账500元"
    assert context["turns"][-1]["content"] == request["message"]


def test_bank_funding_vocabulary_and_amount_ordering_cover_common_business_expressions():
    explicit = [
        "向我的账户转入500元",
        "往我的银行卡打500元",
        "给自己的卡打款500元",
        "向本人账户汇入五百元",
        "往我自己的账户转入500元",
        "向本人名下账户汇款500元",
        "往我这张卡汇款500元",
        "给我的银行卡充值500",
        "往我的账户充钱500元",
        "往我的账户存500元",
        "给本人账户划入500元",
        "把500元打到我的银行卡里",
        "把500元汇到本人账户",
        "将500元存进自己的账户",
        "转500元到我的账户",
        "从尾号1234的银行卡往我的账户转入500元",
    ]
    relative = [
        "向里面转账500元",
        "往账户里打500元",
        "往这张卡存500元",
        "给这个账户充500元",
        "把500元打进里面",
        "先充值500元",
        "那先补500进去",
        "转进来500元",
        "先存500元",
    ]
    for text in explicit:
        assert incoming_intent(text) == {"amount_fen": 50000}, text
    for text in relative:
        assert incoming_intent(text, own_account_context=True) == {"amount_fen": 50000}, text
        assert incoming_intent(text) == {"amount_fen": 50000, "clarify_destination": True}, text


def test_funding_channel_currency_and_unconnected_own_accounts_are_distinguished(client, headers):
    for text, expected in [
        ("从我的账户转到自己的另一张银行卡500元", "本人不同账户"),
        ("往我的账户存500元现金", "现金存款"),
        ("往我的账户汇入500美元", "外币"),
        ("明天往我的银行卡打500元", "预约转入"),
        ("往尾号1234的银行卡转500元", "尾号"),
    ]:
        response = ask(client, headers, text)
        assert response["status"] == "INCOMING_GUIDANCE" and "ui_action" not in response, text
        assert expected in response["message"], text
    for text in [
        "给我的卡充值500元话费",
        "给我的卡充值500元到支付宝",
        "把余额改成500元",
        "向我的账户转入500元 access_token=do-not-store",
    ]:
        assert incoming_intent(text, own_account_context=True) is None, text
    assert balance(client, headers) == 0


def test_destination_answer_can_switch_to_verified_recipient_without_losing_amount(client, headers):
    question = ask(client, headers, "向里面转账500元")
    answered = ask(client, headers, "给妈妈", question["conversation_id"])
    assert answered["nodes"][0]["operation"] == "funds.transfer"
    assert answered["nodes"][0]["params"]["recipient_hint"] == "妈妈"
    assert answered["nodes"][0]["params"]["amount_fen"] == 50000
    assert answered["status"] == "FAILED" and balance(client, headers) == 0


def test_explicit_shortfall_uses_current_available_funds_and_still_requires_confirmation(client, headers):
    from conftest import payroll

    payroll(client, headers, 10000)
    blocked = ask(client, headers, "给妈妈转500元")
    request = ask(client, headers, "先补足差额", blocked["conversation_id"])
    assert request["ui_action"]["amount_fen"] == 40000 and balance(client, headers) == 10000
    ambiguous = ask(client, headers, "先补点钱", blocked["conversation_id"])
    assert ambiguous["status"] == "NEEDS_CLARIFICATION" and "多少元" in ambiguous["message"]


def test_switching_to_another_transfer_draft_does_not_bind_the_previous_blocked_transfer(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    question = ask(client, headers, "给房东转账", blocked["conversation_id"])
    assert question["status"] == "NEEDS_CLARIFICATION"
    request = ask(client, headers, "向里面转账500元", blocked["conversation_id"])
    assert request["status"] == "NEEDS_CLARIFICATION" and "自己的银行账户" in request["message"]
    assert "ui_action" not in request


def test_funding_history_and_howto_do_not_prepare_another_deposit(client, headers):
    for text in ["查询向我的账户转入500元是否到账", "查一下转入500元的记录", "看看往我的卡打500元的回执"]:
        assert incoming_intent(text, own_account_context=True) is None
    for text, expected in [("怎么往我的卡转入500元", "转出银行"), ("存现金500元", "现金存款")]:
        response = ask(client, headers, text)
        assert response["status"] == "INCOMING_GUIDANCE" and "ui_action" not in response
        assert expected in response["message"]
    assert balance(client, headers) == 0


def test_existing_legacy_unsupported_funding_reply_can_recover_same_failed_plan(client, headers):
    blocked = ask(client, headers, "给妈妈转500元")
    client.app.state.graph.start(
        "u_demo",
        "向里面转账500元",
        blocked["conversation_id"],
        intent=Intent(summary="旧版本误拒绝", reject_reason="暂未支持本人转入", rejection_kind="business"),
    )
    request = ask(client, headers, "向里面转账500元", blocked["conversation_id"])
    assert request["status"] == "WAIT_DEMO_CONFIRM"
    result = decide(client, headers, request).json()
    assert result["continuation"]["plan_id"] == blocked["id"] and balance(client, headers) == 50000
