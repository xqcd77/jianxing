from bank_agent.businesses import BUSINESSES
from bank_agent.operations import DEFINITIONS
from conftest import approve, finish, login, payroll, start


def prepare(client, headers, category, action, params=None):
    return client.post(f"/api/businesses/{category}/{action}/prepare", headers=headers, json={"params": params or {}})


def test_six_categories_and_every_existing_operation_have_correct_entries(client, headers):
    assert client.get("/api/businesses").status_code == 401
    response = client.get("/api/businesses", headers=headers)
    assert response.status_code == 200
    catalog = response.json()
    assert [c["label"] for c in catalog] == ["智能转账", "账单分析", "理财操作", "卡片管理", "订阅代扣", "跨场景联动"]
    assert {a["operation"] for c in BUSINESSES for a in c["actions"] if a["operation"]} == set(DEFINITIONS)
    funds = next(c for c in catalog if c["id"] == "funds")
    assert [a["id"] for a in funds["actions"]][-2:] == ["aa", "budget"]
    assert [a["id"] for a in funds["actions"][-2]["children"]] == ["aa_create", "aa_manage"]
    assert [a["id"] for c in catalog if c["id"] == "life" for a in c["actions"]] == ["allocation", "budget_cashflow", "overview", "plans"]
    assert len({(c["id"], a["id"]) for c in catalog for a in c["actions"]}) == sum(len(c["actions"]) for c in catalog)
    assert len(BUSINESSES) == 6


def test_queries_and_subscription_contract_query_keep_their_actual_permissions(client, headers):
    for category, action in [
        ("funds", "balance"),
        ("bills", "anomalies"),
        ("wealth", "compare"),
        ("cards", "query"),
        ("subscriptions", "detect"),
        ("life", "orders"),
    ]:
        response = prepare(client, headers, category, action)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "COMPLETED"
    contracts = prepare(client, headers, "subscriptions", "query").json()
    assert contracts["status"] == "WAIT_CONFIRM" and contracts["nodes"][0]["tier"] == "YELLOW"
    assert finish(client, headers, contracts)["status"] == "COMPLETED"


def test_month_and_year_entries_do_not_mix_reporting_periods(client, headers):
    month = prepare(client, headers, "bills", "monthly", {"period": "2026-09"}).json()
    year = prepare(client, headers, "bills", "annual", {"period": "2026"}).json()
    assert not month["nodes"][0]["params"]["annual"] and year["nodes"][0]["params"]["annual"]
    tampered = prepare(client, headers, "bills", "monthly", {"period": "2026-09", "annual": True})
    assert tampered.status_code == 400


def test_transfer_and_scheduled_transfer_preserve_details_and_need_confirmation(client, headers):
    payroll(client, headers)
    transfer = prepare(client, headers, "funds", "transfer", {"recipient_hint": "妈妈", "amount_fen": 50000}).json()
    assert transfer["status"] == "WAIT_CONFIRM" and transfer["nodes"][0]["params"]["amount_fen"] == 50000
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
    scheduled = prepare(
        client,
        headers,
        "funds",
        "scheduled_transfer",
        {"recipient_hint": "妈妈", "amount_fen": 50000, "scheduled_for": "2026-10-15T09:00:00+08:00"},
    )
    assert scheduled.status_code == 200 and scheduled.json()["status"] == "WAIT_TIME"
    for when in [None, "2026-10-12T09:00:00+08:00", "2026-10-15T09:00:00"]:
        assert (
            prepare(
                client,
                headers,
                "funds",
                "scheduled_transfer",
                {"recipient_hint": "妈妈", "amount_fen": 50000, "scheduled_for": when},
            ).status_code
            == 400
        )
    assert (
        prepare(
            client, headers, "funds", "transfer", {"recipient_hint": "妈妈", "amount_fen": 50000, "confirmed": True}
        ).status_code
        == 400
    )


def test_incoming_entry_is_only_preparation_and_non_admin_gets_guidance(client, headers):
    prepared = prepare(client, headers, "funds", "incoming", {"amount_fen": 50000}).json()
    assert prepared["status"] == "WAIT_DEMO_CONFIRM"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 0
    assert prepare(client, headers, "funds", "incoming", {"amount_fen": 0}).status_code == 400
    assert prepare(client, headers, "funds", "incoming", {"amount_fen": 50000, "admin": True}).status_code == 400
    assert (
        prepare(client, login(client, "wang"), "funds", "incoming", {"amount_fen": 50000}).json()["status"]
        == "INCOMING_GUIDANCE"
    )
    wang = login(client, "wang")
    details = client.get("/api/businesses/funds/incoming", headers=wang).json()
    assert not details["fields"] and details["tier"] == "GREEN"
    assert prepare(client, wang, "funds", "incoming").json()["status"] == "INCOMING_GUIDANCE"
    pending = client.post("/api/chat", headers=headers, json={"message": "我要向我的账户转入资金"}).json()
    cid = pending["conversation_id"]
    query = client.post(
        "/api/businesses/bills/monthly/prepare",
        headers=headers,
        json={"params": {"period": "2026-09"}, "conversation_id": cid},
    )
    assert query.status_code == 200 and query.json()["status"] == "COMPLETED"
    amount = client.post("/api/chat", headers=headers, json={"message": "500元", "conversation_id": cid}).json()
    assert "ui_action" not in amount


def test_aa_entry_includes_creator_and_only_exposes_members_pending_share(client, headers):
    request = prepare(
        client,
        headers,
        "funds",
        "aa_create",
        {"amount_fen": 30001, "participants": ["u_wang", "u_li"], "subject": "聚餐"},
    ).json()
    completed = finish(client, headers, request)
    data = completed["nodes"][0]["result"]["data"]
    assert [s["amount_fen"] for s in data["shares"]] == [10001, 10000, 10000]
    assert data["shares"][0]["status"] == "SELF_SHARE"
    own = client.get("/api/businesses/funds/aa_pay", headers=headers).json()
    assert not own["available"]
    wang = login(client, "wang")
    other = client.get("/api/businesses/funds/aa_pay", headers=wang).json()
    assert other["fields"][0]["choices"][0]["value"] == data["request_id"]
    paid = finish(
        client, wang, prepare(client, wang, "funds", "aa_pay", {"request_id": data["request_id"]}).json(), "u_wang"
    )
    assert paid["status"] == "COMPLETED"


def test_wealth_has_no_default_answers_and_only_owned_holdings_for_redemption(client, headers):
    metadata = client.get("/api/businesses/wealth/assess", headers=headers).json()
    assert "default" not in metadata["fields"][0]
    assert prepare(client, headers, "wealth", "assess", {"answers": [1, 2]}).status_code == 400
    assert not client.get("/api/businesses/wealth/redeem", headers=headers).json()["available"]
    payroll(client, headers)
    purchase = prepare(client, headers, "wealth", "purchase", {"product_id": "P_CASH", "amount_fen": 50000}).json()
    assert purchase["status"] == "WAIT_MFA"
    finish(client, headers, purchase)
    redeem = client.get("/api/businesses/wealth/redeem", headers=headers).json()
    assert redeem["fields"][0]["choices"][0]["value"] == "P_CASH"


def test_restore_and_unlock_are_distinct_and_bound_to_owned_card_state(client, headers):
    assert prepare(client, headers, "cards", "apply", {"kind": "invalid", "name": "测试卡"}).status_code == 400
    assert not client.get("/api/businesses/cards/restore", headers=headers).json()["available"]
    lost = prepare(client, headers, "cards", "lost", {"card_id": "C_MAIN"}).json()
    assert lost["status"] == "WAIT_MFA"
    finish(client, headers, lost)
    assert (
        client.get("/api/businesses/cards/restore", headers=headers).json()["fields"][0]["choices"][0]["value"]
        == "C_MAIN"
    )
    assert prepare(client, headers, "cards", "unlock", {"card_id": "C_MAIN"}).status_code == 404
    restore = prepare(client, headers, "cards", "restore", {"card_id": "C_MAIN"}).json()
    assert restore["status"] == "WAIT_MFA"
    finish(client, headers, restore)
    assert client.get("/api/businesses/cards/unlock", headers=headers).json()["available"]


def test_cancel_contract_scope_is_explicit_and_preserves_other_side(client, headers):
    metadata = client.get("/api/businesses/subscriptions/cancel", headers=headers).json()
    assert "default" not in metadata["fields"][1]
    assert set(metadata["fields"][0]["choices"][0]) == {"value", "label"}
    result = finish(
        client,
        headers,
        prepare(client, headers, "subscriptions", "cancel", {"subscription_id": "S_VIDEO", "scope": "bank"}).json(),
    )
    data = result["nodes"][0]["result"]["data"]
    assert data["bank_mandate"] == "REVOKED" and data["merchant_contract"] == "ACTIVE" and data["refund_fen"] == 0
    assert (
        prepare(
            client, login(client, "wang"), "subscriptions", "cancel", {"subscription_id": "S_VIDEO", "scope": "both"}
        ).status_code
        == 404
    )


def test_birthday_creates_dependencies_and_checkout_resumes_existing_plan(client, headers):
    payroll(client, headers)
    setup = prepare(
        client,
        headers,
        "life",
        "birthday",
        {"birthday_date": "2026-10-20", "budget_fen": 100000, "address": "测试演示地址", "card_id": "C_MAIN"},
    ).json()
    waiting = finish(client, headers, setup)
    assert waiting["status"] == "WAIT_TIME" and len(waiting["nodes"]) == 3
    response = prepare(client, headers, "life", "birthday_checkout", {"plan_id": waiting["id"]})
    assert response.status_code == 200 and response.json()["id"] == waiting["id"]
    assert len(response.json()["nodes"]) == 3
    assert (
        prepare(client, login(client, "wang"), "life", "birthday_checkout", {"plan_id": waiting["id"]}).status_code
        == 404
    )
    assert (
        prepare(
            client,
            headers,
            "life",
            "birthday",
            {"birthday_date": "2026-11-01", "budget_fen": 100000, "address": "演示地址", "card_id": "C_MAIN"},
        ).status_code
        == 400
    )


def test_combined_query_is_six_domains_and_unknown_business_cannot_run(client, headers):
    combined = prepare(client, headers, "life", "overview").json()
    assert combined["status"] == "COMPLETED"
    assert {n["agent"] for n in combined["nodes"]} == {"funds", "bills", "wealth", "cards", "subscriptions", "life"}
    assert prepare(client, headers, "wealth", "delete_all").status_code == 404
    assert prepare(client, headers, "life", "overview", {"confirmed": True}).status_code == 400


def test_release_list_changes_only_after_authorized_budget_and_is_user_scoped(client, headers):
    payroll(client, headers)
    assert not client.get("/api/businesses/funds/release", headers=headers).json()["available"]
    plan = start(client, "funds.reserve", {"amount_fen": 100000, "purpose": "生活费"})
    approved = approve(client, headers, plan).json()
    hold_id = approved["nodes"][0]["result"]["data"]["hold_id"]
    assert (
        client.get("/api/businesses/funds/release", headers=headers).json()["fields"][0]["choices"][0]["value"]
        == hold_id
    )
    assert prepare(client, login(client, "wang"), "funds", "release", {"hold_id": hold_id}).status_code == 404
