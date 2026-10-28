from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from bank_agent.storage import Account, LedgerEntry
from conftest import finish, login, payroll, start


def test_salary_birthday_minimum_loop(client, headers):
    payroll(client, headers)
    plan = client.post("/api/scenarios/salary", headers=headers).json()
    assert plan["review"]["business_service_count"] == 4
    assert plan["review"]["orchestration_roles"] == ["supervisor", "business", "verifier"]
    plan = finish(client, headers, plan)
    assert plan["status"] == "WAIT_TIME"
    account = client.get("/api/overview", headers=headers).json()["account"]
    assert account == {"id": "a_demo", "balance_fen": 470000, "reserved_fen": 400000, "available_fen": 70000}
    result = client.post("/api/demo/clock", headers=headers, json={"days": 5})
    assert result.status_code == 200
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    purchase = next(n for n in plan["nodes"] if n["operation"] == "life.purchase")
    assert purchase["status"] == "WAIT_CONFIRM"
    assert purchase["params"]["quote"]["total_fen"] == 50000
    plan = finish(client, headers, plan)
    assert plan["status"] == "COMPLETED"
    orders = next(n["result"]["data"]["orders"] for n in plan["nodes"] if n["operation"] == "life.purchase")
    assert len(orders) == 2 and all(o["status"] == "CONFIRMED" for o in orders)
    account = client.get("/api/overview", headers=headers).json()["account"]
    assert (account["balance_fen"], account["reserved_fen"], account["available_fen"]) == (420000, 300000, 120000)
    assert client.get("/api/audit", headers=headers).json()["chain_verified"]


def test_business_agent_uses_all_six_rule_services(client, headers):
    payroll(client, headers)
    plan = client.post("/api/scenarios/six", headers=headers).json()
    assert plan["review"]["business_service_count"] == 6
    assert plan["review"]["agent_count"] == 3
    assert {n["agent"] for n in plan["nodes"]} == {"funds", "bills", "wealth", "cards", "subscriptions", "life"}
    plan = finish(client, headers, plan)
    assert all(
        n["status"] == "SUCCEEDED"
        for n in plan["nodes"]
        if n["operation"] in ["balance.query", "bills.report", "wealth.compare", "cards.query", "subscriptions.detect"]
    )


def test_green_queries_report_and_duplicate_detection(client, headers):
    plan = start(client, "bills.report", {"period": "2026", "annual": True})
    data = plan["nodes"][0]["result"]["data"]
    assert plan["status"] == "COMPLETED" and data["income_fen"] == 5400000
    assert data["consumption_fen"] == 5220000 and data["transfer_fen"] == 180000
    assert len(data["covered_months"]) == 9
    data = start(client, "bills.anomalies")["nodes"][0]["result"]["data"]
    assert len(data["alerts"]) == 1 and len(data["alerts"][0]["transaction_ids"]) == 2


def test_transfer_name_phone_note_and_context(client, headers):
    payroll(client, headers)
    for hint in ["妈妈", "13800000001", "生活费"]:
        plan = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": hint, "amount_fen": 1000}))
        assert plan["status"] == "COMPLETED"
    plan = client.post(
        "/api/chat", headers=headers, json={"message": "再转给她20元", "conversation_id": plan["conversation_id"]}
    ).json()
    assert plan["nodes"][0]["params"]["recipient_hint"] == "妈妈"
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_unknown_recipient_needs_clarification(client, headers):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"recipient_hint": "张三", "amount_fen": 1000})
    assert plan["status"] == "WAIT_INPUT" and plan["nodes"][0]["result"] is None
    node = plan["nodes"][0]
    plan = client.post(
        "/api/plans/" + plan["id"] + "/edit",
        headers=headers,
        json={"node_id": node["id"], "params": {"recipient_hint": "妈妈", "amount_fen": 1000}},
    ).json()
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_scheduled_transfer_rechecks_day_limit_and_needs_new_confirmation(client, headers):
    payroll(client, headers)
    plan = start(
        client,
        "funds.transfer",
        {"recipient_hint": "妈妈", "amount_fen": 80000},
        scheduled_for="2026-10-14T09:00:00+08:00",
    )
    assert plan["status"] == "WAIT_TIME"
    client.post("/api/demo/clock", headers=headers, json={"days": 1})
    plan = client.get("/api/plans/" + plan["id"], headers=headers).json()
    assert plan["status"] == "WAIT_CONFIRM"
    finish(client, headers, plan)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 30000})
    assert plan["nodes"][0]["tier"] == "RED"
    assert finish(client, headers, plan)["status"] == "COMPLETED"


def test_aa_exact_cents_and_authenticated_participant_payment(client, headers):
    plan = finish(client, headers, client.post("/api/scenarios/aa", headers=headers).json())
    data = plan["nodes"][0]["result"]["data"]
    shares = data["shares"]
    assert sum(s["amount_fen"] for s in shares) == 30001
    assert [s["amount_fen"] for s in shares] == [10001, 10000, 10000]
    for username, user_id in [("wang", "u_wang"), ("li", "u_li")]:
        auth = login(client, username)
        plan = finish(
            client, auth, start(client, "aa.pay", {"request_id": data["request_id"]}, user_id=user_id), user_id
        )
        assert plan["status"] == "COMPLETED"
    assert plan["nodes"][0]["result"]["data"]["settled"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 20000


def test_wealth_purchase_redeem_delayed_cash_and_settlement(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_STEADY", "amount_fen": 50000}))
    assert plan["status"] == "COMPLETED"
    plan = finish(client, headers, start(client, "wealth.redeem", {"product_id": "P_STEADY", "amount_fen": 50000}))
    data = plan["nodes"][0]["result"]["data"]
    assert data["status"] == "PENDING" and data["settles_on"] == "2026-10-14"
    assert client.get("/api/overview", headers=headers).json()["account"]["available_fen"] == 550000
    client.post("/api/demo/clock", headers=headers, json={"days": 1})
    assert client.get("/api/overview", headers=headers).json()["account"]["available_fen"] == 600000
    client.post("/api/scheduler/tick", headers=headers)
    assert client.get("/api/overview", headers=headers).json()["account"]["available_fen"] == 600000


def test_risk_assessment_and_lock_period(client, headers):
    payroll(client, headers)
    rejected = finish(
        client, headers, start(client, "wealth.purchase", {"product_id": "P_LOCKED", "amount_fen": 50000})
    )
    assert rejected["nodes"][0]["error"]["code"] == "RISK_MISMATCH"
    finish(client, headers, start(client, "wealth.assess", {"answers": [3, 3, 3, 3, 3]}))
    finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_LOCKED", "amount_fen": 50000}))
    plan = finish(client, headers, start(client, "wealth.redeem", {"product_id": "P_LOCKED", "amount_fen": 50000}))
    assert plan["nodes"][0]["error"]["code"] == "PRODUCT_LOCKED"


def test_card_apply_limit_restrict_lost_restore(client, headers):
    plan = finish(client, headers, client.post("/api/scenarios/card", headers=headers).json())
    assert plan["status"] == "COMPLETED"
    for op, params in [
        ("cards.limit", {"amount_fen": 1200000}),
        ("cards.lost", {}),
        ("cards.restore", {}),
        ("cards.unlock", {}),
    ]:
        plan = finish(client, headers, start(client, op, params))
        assert plan["status"] == "COMPLETED"
    data = plan["nodes"][0]["result"]["data"]
    assert data["status"] == "ACTIVE" and not data["blocked_categories"]


def test_subscription_cancel_no_fake_refund_and_future_no_charge(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, client.post("/api/scenarios/subscriptions", headers=headers).json())
    cancelled = next(n for n in plan["nodes"] if n["operation"] == "subscriptions.cancel")["result"]["data"]
    assert cancelled["bank_mandate"] == "REVOKED" and cancelled["merchant_contract"] == "CANCELLED"
    assert cancelled["refund_fen"] == 0
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
    client.post("/api/demo/clock", headers=headers, json={"days": 5})
    assert client.get("/api/overview", headers=headers).json()["notifications"]
    client.post("/api/demo/clock", headers=headers, json={"days": 2})
    info = client.get("/api/overview", headers=headers).json()
    assert info["account"]["balance_fen"] == 598100
    assert any(e["subscription_id"] == "S_VIDEO" and e["status"] == "SKIPPED_CANCELLED" for e in info["debit_events"])


def test_cancel_unfinished_plan_releases_holds_preserves_completed_money(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, client.post("/api/scenarios/salary", headers=headers).json())
    plan = client.post("/api/plans/" + plan["id"] + "/cancel", headers=headers).json()
    assert plan["status"] == "CANCELLED"
    info = client.get("/api/overview", headers=headers).json()["account"]
    assert info["balance_fen"] == 470000 and info["reserved_fen"] == 0
    assert any(n["status"] == "SUCCEEDED" for n in plan["nodes"])


def test_duplicate_payroll_and_resume_do_not_duplicate_debit(client, headers):
    first = payroll(client, headers)
    assert payroll(client, headers) == first
    plan = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 10000}))
    for _ in range(3):
        assert client.post("/api/plans/" + plan["id"] + "/resume", headers=headers).json()["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 590000


def test_two_concurrent_debits_cannot_overspend(client, headers):
    payroll(client, headers, 100000)
    plans = [start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}) for _ in range(2)]
    # Both approvals happen before execution, then two processes' equivalent write transactions race.
    from bank_agent.schemas import ApprovalRequest

    for plan in plans:
        node = plan["nodes"][0]
        client.app.state.plans.authorize(
            "u_demo", plan["id"], ApprovalRequest(node_id=node["id"], fingerprint=node["fingerprint"])
        )
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda plan: client.app.state.plans.step("u_demo", plan["id"]), plans))
    outcomes = [client.app.state.plans.snapshot("u_demo", p["id"]) for p in plans]
    assert sum(o["nodes"][0]["status"] == "SUCCEEDED" for o in outcomes) == 1
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 20000


def test_all_ledger_entries_and_account_balances_conserve_money(client, headers):
    payroll(client, headers)
    finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}))
    with client.app.state.db.read() as s:
        assert s.scalar(select(func.sum(Account.balance_fen))) == 0
        assert s.scalar(select(func.sum(LedgerEntry.delta_fen))) == 0
        ledger = dict(
            s.execute(
                select(LedgerEntry.account_id, func.sum(LedgerEntry.delta_fen)).group_by(LedgerEntry.account_id)
            ).all()
        )
        for account in s.scalars(select(Account)):
            assert account.balance_fen == ledger.get(account.id, 0)
