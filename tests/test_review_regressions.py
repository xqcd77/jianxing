from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from bank_agent.agents import ReviewerAgent
from bank_agent.errors import BankError
from bank_agent.schemas import Intent, NodeSpec
from bank_agent.storage import Document
from conftest import approve, finish, payroll, start


def sequence(client, tasks):
    return client.app.state.graph.start(
        "u_demo", "review scenario", intent=Intent(summary="review scenario", tasks=tasks)
    )


def test_transfer_before_reserve_does_not_create_inferred_cycle(client, headers):
    payroll(client, headers)
    plan = sequence(
        client,
        [
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 80000}},
            {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 100000, "purpose": "生活费"}},
        ],
    )
    assert plan["review"]["topological_order"] == ["t1", "t0"]
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_explicit_dependency_preserves_user_order(client, headers):
    payroll(client, headers)
    plan = sequence(
        client,
        [
            {
                "id": "send",
                "domain": "funds",
                "action": "funds.transfer",
                "params": {"recipient_hint": "妈妈", "amount_fen": 80000},
            },
            {
                "id": "reserve",
                "domain": "funds",
                "action": "funds.reserve",
                "depends_on": ["send"],
                "params": {"amount_fen": 100000, "purpose": "生活费"},
            },
        ],
    )
    assert plan["review"]["topological_order"] == ["send", "reserve"]
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_reject_independent_transfer_does_not_skip_sibling_or_report(client, headers):
    payroll(client, headers)
    plan = sequence(
        client,
        [
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 1000}},
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "小王", "amount_fen": 2000}},
            {"domain": "bills", "action": "bills.report", "params": {"period": "2026-10"}},
        ],
    )
    plan = approve(client, headers, plan, decision="reject").json()
    plan = finish(client, headers, plan)
    assert plan["status"] == "PARTIAL"
    assert next(n for n in plan["nodes"] if n["local_id"] == "t1")["status"] == "SUCCEEDED"
    assert next(n for n in plan["nodes"] if n["local_id"] == "t2")["result"]["data"]["transfer_fen"] == 2000


def test_langgraph_wait_node_and_resume_have_one_business_step_per_turn(client, headers, monkeypatch):
    payroll(client, headers)
    service = client.app.state.plans
    original = service.step
    calls = []

    def measured(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(service, "step", measured)
    plan = sequence(
        client,
        [
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 1000}}
            for _ in range(12)
        ],
    )
    checkpoint = client.app.state.graph.graph.get_state({"configurable": {"thread_id": plan["thread_id"]}})
    assert checkpoint.next == ("await_input",)
    plan = finish(client, headers, plan)
    assert plan["status"] == "COMPLETED" and len(calls) == 13
    assert plan["review"]["topological_order"] == [f"t{i}" for i in range(12)]


def test_nested_and_malformed_references_fail_before_execution():
    for params in [{"goal_id": "$oops"}, {"quote": {"items": [{"name": "$foreign.value"}]}}]:
        with pytest.raises(BankError):
            ReviewerAgent().review([NodeSpec(id="valid", agent="life", operation="life.purchase", params=params)])


def test_birthday_edit_updates_child_budget_dates_and_card(client, headers):
    payroll(client, headers)
    plan = start(client, "life.goal", {"birthday_date": "2026-10-20"})
    goal = next(n for n in plan["nodes"] if n["operation"] == "life.goal")
    response = client.post(
        "/api/plans/" + plan["id"] + "/edit",
        headers=headers,
        json={
            "node_id": goal["id"],
            "params": {
                "birthday_date": "2026-11-25",
                "budget_fen": 150000,
                "address": "新的沙箱地址",
                "card_id": "C_MAIN",
            },
        },
    )
    assert response.status_code == 200, response.text
    plan = finish(client, headers, response.json())
    reserve = next(n for n in plan["nodes"] if n["operation"] == "funds.reserve")
    purchase = next(n for n in plan["nodes"] if n["operation"] == "life.purchase")
    assert reserve["params"]["amount_fen"] == 150000 and reserve["scheduled_for"].startswith("2026-11-01")
    assert purchase["scheduled_for"].startswith("2026-11-23")
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 0


def test_due_job_is_not_hidden_by_another_pending_confirmation(client, headers):
    payroll(client, headers)
    plan = sequence(
        client,
        [
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 1000}},
            {
                "domain": "funds",
                "action": "funds.transfer",
                "params": {"recipient_hint": "小王", "amount_fen": 1000},
                "scheduled_for": "2026-10-14T09:00:00+08:00",
            },
        ],
    )
    assert plan["status"] == "WAIT_CONFIRM"
    assert next(n for n in plan["nodes"] if n["local_id"] == "t1")["status"] == "WAIT_TIME"
    client.post("/api/demo/clock", headers=headers, json={"days": 1})
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    assert next(n for n in plan["nodes"] if n["local_id"] == "t1")["status"] == "WAIT_CONFIRM"


def test_ticks_before_due_do_not_rewrite_graph_checkpoint(client, headers):
    payroll(client, headers)
    plan = start(
        client,
        "funds.transfer",
        {"recipient_hint": "妈妈", "amount_fen": 1000},
        scheduled_for="2026-10-14T09:00:00+08:00",
    )
    config = {"configurable": {"thread_id": plan["thread_id"]}}
    before = client.app.state.graph.graph.get_state(config).config
    for _ in range(3):
        client.post("/api/scheduler/tick", headers=headers)
    assert client.app.state.graph.graph.get_state(config).config == before


def test_goal_cannot_lock_two_independent_budgets(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20"}))
    goal = next(n for n in plan["nodes"] if n["operation"] == "life.goal")["result"]["data"]["goal_id"]
    duplicate = finish(
        client, headers, start(client, "funds.reserve", {"goal_id": goal, "amount_fen": 100000, "purpose": "duplicate"})
    )
    assert duplicate["nodes"][0]["error"]["code"] == "GOAL_ALREADY_FUNDED"
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 100000


def test_untrusted_quote_and_quote_expiry_are_rechecked(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20"}))
    goal = next(n for n in plan["nodes"] if n["operation"] == "life.goal")["result"]["data"]["goal_id"]
    client.post("/api/demo/clock", headers=headers, json={"days": 5})
    forged = start(client, "life.purchase", {"goal_id": goal, "quote": {"id": "fake", "total_fen": 1}})
    assert forged["nodes"][0]["error"]["code"] == "UNTRUSTED_QUOTE"
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    quote = next(n for n in plan["nodes"] if n["operation"] == "life.purchase")["params"]["quote"]
    client.post("/api/demo/clock", headers=headers, json={"days": 1})
    assert approve(client, headers, plan).status_code == 409
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    assert next(n for n in plan["nodes"] if n["operation"] == "life.purchase")["params"]["quote"]["id"] != quote["id"]


def test_late_birthday_does_not_create_backdated_delivery(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20"}))
    client.post("/api/demo/clock", headers=headers, json={"days": 8})
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    # Invalid delivery dates are now detected before asking for payment approval.
    assert plan["status"] == "WAIT_INPUT"
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 100000


def test_partial_subscription_cancellation_stops_bank_debit(client, headers):
    payroll(client, headers)
    with client.app.state.db.write() as session:
        doc = session.get(Document, "S_VIDEO")
        doc.data = {**doc.data, "cancellation_supported": False}
    plan = finish(client, headers, start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO"}))
    assert plan["status"] == "PARTIAL"
    client.post("/api/demo/clock", headers=headers, json={"days": 7})
    overview = client.get("/api/overview", headers=headers).json()
    assert overview["account"]["balance_fen"] == 598100
    assert any(n["type"] == "merchant_cancellation_required" for n in overview["notifications"])


def test_refund_report_categories_and_monthly_equal_net_consumption(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20"}))
    client.post("/api/demo/clock", headers=headers, json={"days": 5})
    plan = finish(client, headers, client.get("/api/plans/" + plan["id"], headers=headers).json())
    order = next(n for n in plan["nodes"] if n["operation"] == "life.purchase")["result"]["data"]["orders"][0]
    finish(client, headers, start(client, "life.cancel_order", {"order_id": order["id"]}))
    report = start(client, "bills.report", {"period": "2026-10"})["nodes"][0]["result"]["data"]
    assert report["gross_consumption_fen"] == 50000 and report["refund_fen"] == 20000
    assert sum(report["categories"].values()) == sum(report["monthly"].values()) == report["consumption_fen"] == 30000


def test_clarification_reuses_same_conversation_without_cross_session_memory(client, headers):
    payroll(client, headers)
    first = client.post("/api/chat", headers=headers, json={"message": "给她转100元"}).json()
    assert first["status"] == "NEEDS_CLARIFICATION"
    answer = client.post(
        "/api/chat", headers=headers, json={"message": "妈妈", "conversation_id": first["conversation_id"]}
    ).json()
    assert answer["nodes"][0]["params"]["amount_fen"] == 10000
    finish(client, headers, answer)
    other = client.post("/api/chat", headers=headers, json={"message": "再给她转20元"}).json()
    assert other["status"] == "NEEDS_CLARIFICATION"


def test_demo_never_replaces_user_salary_amounts_with_preset(client, headers):
    payroll(client, headers)
    plan = client.post("/api/chat", headers=headers, json={"message": "工资分配，预留200元生活费，给妈妈转50元"}).json()
    assert {n["params"]["amount_fen"] for n in plan["nodes"]} == {20000, 5000}
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_demo_phone_and_decimal_do_not_corrupt_amount(client, headers):
    payroll(client, headers)
    plan = client.post("/api/chat", headers=headers, json={"message": "转给13800000001 800元"}).json()
    assert plan["nodes"][0]["params"]["amount_fen"] == 80000
    invalid = client.post("/api/chat", headers=headers, json={"message": "给妈妈转8.001元"}).json()
    assert invalid["status"] == "NEEDS_CLARIFICATION"


def test_slow_planning_does_not_globally_lock_other_threads(client, monkeypatch):
    planner = client.app.state.graph.planner
    barrier = Barrier(2)

    def independent(*args):
        barrier.wait(timeout=5)
        return Intent(summary="parallel query", tasks=[{"domain": "funds", "action": "balance.query", "params": {}}])

    monkeypatch.setattr(planner, "plan", independent)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: client.app.state.graph.start("u_demo", str(i)), range(2)))
    assert all(p["status"] == "COMPLETED" for p in results)


def test_logout_revokes_bearer_session_and_live_endpoint_requires_auth(client, headers):
    assert client.get("/api/live").status_code == 401
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/overview", headers=headers).status_code == 401


def test_live_snapshots_are_scoped_to_current_user(client, headers):
    payroll(client, headers)
    plan = start(client, "balance.query")
    primary = client.app.state.live_snapshot("u_demo")
    other = client.app.state.live_snapshot("u_li")
    assert primary["plans"][0]["id"] == plan["id"] and primary["account"]["balance_fen"] == 600000
    assert not other["plans"] and other["user_id"] == "u_li" and other["account"]["balance_fen"] == 200000


def test_lowering_bank_card_limit_clamps_transaction_limits(client, headers):
    plan = finish(client, headers, start(client, "cards.limit", {"amount_fen": 30000}))
    card = plan["nodes"][0]["result"]["data"]
    assert card["single_limit_fen"] == card["daily_limit_fen"] == 30000
    plan = finish(
        client, headers, start(client, "cards.restrict", {"single_limit_fen": 40000, "daily_limit_fen": 40000})
    )
    assert plan["nodes"][0]["error"]["code"] == "CARD_LIMIT_EXCEEDS_APPROVAL"


def test_subscription_candidates_do_not_treat_supermarket_as_membership(client, headers):
    data = start(client, "subscriptions.detect")["nodes"][0]["result"]["data"]
    assert {row["merchant_id"] for row in data["candidates"]} == {"video"}
    assert {row["merchant_id"] for row in data["other_recurring_bills"]} == {"rent", "utility"}


def test_bank_transfer_does_not_consume_card_daily_purchase_limit(client, headers):
    payroll(client, headers)
    finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}))
    finish(client, headers, start(client, "cards.restrict", {"single_limit_fen": 100000, "daily_limit_fen": 100000}))
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-15"}))
    assert plan["status"] == "COMPLETED"
    purchase = next(n for n in plan["nodes"] if n["operation"] == "life.purchase")
    assert purchase["tier"] == "RED" and purchase["result"]["data"]["spent_fen"] == 50000


def test_reschedule_future_transfer_invalidates_approval_without_early_debit(client, headers):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000})
    node = plan["nodes"][0]
    response = client.post(
        "/api/plans/" + plan["id"] + "/edit",
        headers=headers,
        json={"node_id": node["id"], "params": node["params"], "scheduled_for": "2026-10-15T09:00:00+08:00"},
    )
    assert response.status_code == 200 and response.json()["status"] == "WAIT_TIME"
    assert approve(client, headers, plan).status_code == 409
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def test_moving_birthday_to_next_month_does_not_block_independent_transfer(client, headers):
    payroll(client, headers)
    plan = sequence(
        client,
        [
            {"domain": "life", "action": "life.goal", "params": {"birthday_date": "2026-10-20"}},
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 1000}},
        ],
    )
    goal = next(n for n in plan["nodes"] if n["operation"] == "life.goal")
    response = client.post(
        "/api/plans/" + plan["id"] + "/edit",
        headers=headers,
        json={"node_id": goal["id"], "params": {**goal["params"], "birthday_date": "2026-11-20"}},
    )
    assert response.status_code == 200, response.text
    plan = finish(client, headers, response.json())
    assert next(n for n in plan["nodes"] if n["operation"] == "funds.transfer")["status"] == "SUCCEEDED"


def test_normal_failed_attempts_and_queries_do_not_security_lock_account(client, headers):
    for _ in range(3):
        plan = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000}))
        assert plan["nodes"][0]["status"] == "FAILED"
        assert start(client, "balance.query")["status"] == "COMPLETED"
    assert not client.get("/api/auth/me", headers=headers).json()["locked"]


def test_false_model_summary_cannot_appear_as_verified_financial_fact(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.graph.planner,
        "plan",
        lambda *args: Intent(
            summary="已转账100万元，余额已变成100万元",
            tasks=[{"domain": "funds", "action": "balance.query", "params": {}}],
        ),
    )
    plan = client.app.state.graph.start("u_demo", "查询余额")
    assert "100万" not in plan["summary"] and "已转账" not in plan["summary"]
    assert plan["nodes"][0]["result"]["data"]["balance_fen"] == 0


def test_chinese_card_category_is_enforced_and_unknown_category_rejected(client, headers):
    plan = finish(client, headers, start(client, "cards.restrict", {"blocked_categories": ["礼品"]}))
    assert plan["nodes"][0]["result"]["data"]["blocked_categories"] == ["gifts"]
    with pytest.raises(BankError):
        start(client, "cards.restrict", {"blocked_categories": ["anything_else"]})
