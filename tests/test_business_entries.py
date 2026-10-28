"""Exercise every visible business entry through the real preparation and authorization APIs."""
import pytest
from sqlalchemy import func, select

from bank_agent.businesses import MENU
from bank_agent.storage import LedgerEntry, Plan
from conftest import login, payroll, start


CASES = [
    ("funds", key) for key in ["balance", "transfer", "scheduled_transfer", "incoming", "aa_create", "aa_manage", "reserve", "release"]
] + [("bills", key) for key in MENU["bills"]] + [("wealth", key) for key in MENU["wealth"]] + [
    ("cards", key) for key in MENU["cards"]
] + [("subscriptions", key) for key in MENU["subscriptions"]] + [("life", key) for key in MENU["life"]]


def prepare(client, headers, category, action, params=None):
    response = client.post(f"/api/businesses/{category}/{action}/prepare", headers=headers, json={"params": params or {}})
    assert response.status_code == 200, response.text
    return response.json()


def complete(client, headers, plan):
    for _ in range(20):
        node = next((n for n in plan["nodes"] if n["status"] in {"WAIT_CONFIRM", "WAIT_MFA"}), None)
        if not node:
            return plan
        payload = {"node_id": node["id"], "fingerprint": node["fingerprint"]}
        if node["tier"] == "RED":
            code = client.post("/api/security/demo-code", headers=headers, json={
                "purpose": "approve", "plan_id": plan["id"], "node_id": node["id"], "fingerprint": node["fingerprint"],
            })
            assert code.status_code == 200, code.text
            payload.update(otp=code.json()["code"], demo_challenge_id=code.json()["challenge_id"])
        approved = client.post(f"/api/plans/{plan['id']}/approve", headers=headers, json=payload)
        assert approved.status_code == 200, approved.text
        plan = approved.json()
    raise AssertionError("business did not converge")


def bank_account(client, headers):
    return client.get("/api/overview", headers=headers).json()["account"]


def test_all_visible_leaf_entries_have_execution_cases(client, headers):
    categories = client.get("/api/businesses", headers=headers).json()
    leaf = {(c["id"], a["id"]) for c in categories for item in c["actions"] for a in (item["children"] if item.get("mode") == "group" else [item])}
    assert leaf == set(CASES) and len(leaf) == 33


@pytest.mark.parametrize("category,action", CASES, ids=[f"{c}-{a}" for c, a in CASES])
def test_each_business_entry_closed_loop(client, headers, category, action):
    payroll(client, headers, amount=3_000_000)
    params = {}
    if action in {"transfer", "scheduled_transfer"}:
        params = {"recipient_hint": "妈妈", "amount_fen": 50000}
        if action == "scheduled_transfer":
            params["scheduled_for"] = "2026-10-14T09:00:00+08:00"
    if action == "incoming":
        params = {"amount_fen": 1_000_000}
    if action == "aa_create":
        params = {"amount_fen": 30001, "participants": ["u_wang", "u_li"], "subject": "共同用餐"}
    if action == "aa_manage":
        creation = prepare(client, headers, "funds", "aa_create", {"amount_fen": 30001, "participants": ["u_wang", "u_li"], "subject": "共同用餐"})
        request = complete(client, headers, creation)["nodes"][0]["result"]["data"]
    if action == "reserve":
        params = {"amount_fen": 100000, "purpose": "房租备用"}
    if action == "release":
        reserved = complete(client, headers, start(client, "funds.reserve", {"amount_fen": 100000, "purpose": "房租备用"}))
        params = {"hold_id": reserved["nodes"][0]["result"]["data"]["hold_id"]}
    if category == "bills":
        params = {
            "monthly": {"period": "2026-09"}, "annual": {"period": "2026"},
            "cashflow": {"days": 30, "income_delay_days": 3, "expense_increase_pct": 10},
            "income_plan": {"amount_fen": 600000, "date": "2026-10-30", "label": "预计工资"},
        }.get(action, {})
    if category == "wealth":
        if action == "assess":
            params = {"answers": [2, 2, 2, 2, 2]}
        if action == "purchase":
            params = {"product_id": "P_CASH", "amount_fen": 50000}
        if action in {"redeem", "holdings"}:
            complete(client, headers, start(client, "wealth.purchase", {"product_id": "P_STEADY", "amount_fen": 50000}))
            if action == "redeem":
                params = {"product_id": "P_STEADY", "amount_fen": 50000}
    if category == "cards":
        if action == "apply":
            params = {"kind": "virtual", "name": "日常付款卡"}
        if action in {"limit", "restrict", "unlock", "lost", "restore"}:
            params = {"card_id": "C_MAIN"}
            if action == "limit":
                params["amount_fen"] = 1200000
            if action in {"restrict", "unlock"}:
                restrictions = {"card_id": "C_MAIN", "single_limit_fen": 50000, "daily_limit_fen": 100000, "blocked_categories": ["gaming"]}
                if action == "restrict":
                    params = restrictions
                else:
                    complete(client, headers, start(client, "cards.restrict", restrictions))
            if action == "restore":
                complete(client, headers, start(client, "cards.lost", {"card_id": "C_MAIN"}))
    if category == "subscriptions":
        if action == "cancel":
            params = {"subscription_id": "S_VIDEO", "scope": "both"}
        if action == "remind":
            params = {"subscription_id": "S_VIDEO", "days_before": 2}
    if category == "life":
        if action == "allocation":
            params = {"allocation_kind": "budget_transfer_investment", "budget_fen": 300000, "purpose": "生活备用金",
                      "recipient_hint": "妈妈", "transfer_fen": 80000, "product_id": "P_CASH", "investment_fen": 50000}
        if action == "budget_cashflow":
            params = {"amount_fen": 100000, "purpose": "出行备用金", "days": 30}
        if action == "plans":
            waiting = prepare(client, headers, "funds", "scheduled_transfer", {"recipient_hint": "妈妈", "amount_fen": 50000, "scheduled_for": "2026-10-14T09:00:00+08:00"})
    before = bank_account(client, headers)
    result = prepare(client, headers, category, action, params)
    if action == "incoming":
        assert result["status"] == "WAIT_DEMO_CONFIRM" and bank_account(client, headers) == before
        path = f"/api/demo/incoming/{result['ui_action']['request_id']}/decision"
        assert client.post(path, headers=headers, json={"decision": "confirm"}).status_code == 200
        assert bank_account(client, headers)["balance_fen"] == before["balance_fen"] + 1_000_000
    elif action == "aa_manage":
        assert result["status"] == "AA_RECORDS" and result["items"][0]["my_status"] == "SELF_SHARE"
        assert result["items"][0]["receivable_fen"] == 20000 and result["items"][0]["collected_fen"] == 0
        wang = login(client, "wang")
        mine = prepare(client, wang, "funds", "aa_manage")
        assert mine["items"][0]["my_status"] == "UNPAID"
        paid = complete(client, wang, prepare(client, wang, "funds", "aa_pay", {"request_id": request["request_id"]}))
        assert paid["status"] == "COMPLETED"
        progress = prepare(client, headers, "funds", "aa_status", {"request_id": request["request_id"]})
        assert progress["nodes"][0]["result"]["data"]["shares"][1]["status"] == "PAID"
        assert prepare(client, headers, "funds", "aa_manage")["items"][0]["collected_fen"] == 10000
        assert bank_account(client, headers)["balance_fen"] == before["balance_fen"] + 10000
    elif action == "plans":
        assert result["status"] == "PLAN_RECORDS" and waiting["id"] in [p["id"] for p in result["items"]]
        resumed = client.post(f"/api/plans/{waiting['id']}/resume", headers=headers, json={}).json()
        assert resumed["status"] == "WAIT_TIME" and bank_account(client, headers) == before
        cancel = client.post(f"/api/plans/{waiting['id']}/cancel", headers=headers, json={})
        assert cancel.status_code == 200 and cancel.json()["status"] == "CANCELLED"
    else:
        if action == "scheduled_transfer":
            assert result["status"] == "WAIT_TIME" and bank_account(client, headers) == before
            assert client.post("/api/demo/clock", headers=headers, json={"days": 1}).status_code == 200
            result = client.get(f"/api/plans/{result['id']}", headers=headers).json()
        if any(n["tier"] != "GREEN" for n in result["nodes"]):
            assert bank_account(client, headers) == before
        done = complete(client, headers, result)
        assert done["status"] == "COMPLETED", done
        assert all(n["status"] == "SUCCEEDED" and n["result"] for n in done["nodes"])
        data = done["nodes"][0]["result"]["data"]
        after = bank_account(client, headers)
        if action in {"transfer", "scheduled_transfer", "purchase"}:
            assert after["balance_fen"] == before["balance_fen"] - params["amount_fen"]
        elif action == "reserve":
            assert after["balance_fen"] == before["balance_fen"] and after["reserved_fen"] == 100000
        elif action == "release":
            assert after["balance_fen"] == before["balance_fen"] and after["reserved_fen"] == 0
        elif action == "allocation":
            assert after["balance_fen"] == before["balance_fen"] - 130000 and after["reserved_fen"] == 300000
            assert not any(n["operation"].startswith("life.") for n in done["nodes"])
            assert next(n for n in done["nodes"] if n["operation"] == "cashflow.project")["result"]["data"]["available_fen"] == after["available_fen"]
        elif action == "budget_cashflow":
            assert after["balance_fen"] == before["balance_fen"] and after["reserved_fen"] == 100000
            assert done["nodes"][1]["result"]["data"]["available_fen"] == after["available_fen"]
        elif action == "redeem":
            assert data["status"] == "PENDING" and after == before
            client.post("/api/demo/clock", headers=headers, json={"days": 1})
            assert bank_account(client, headers)["balance_fen"] == before["balance_fen"] + data["amount_fen"]
        else:
            assert after == before
        if action in {"monthly", "annual"}:
            assert data["period"] == params["period"] and data["annual"] == (action == "annual")
        if action == "anomalies":
            assert len(data["alerts"]) == 1
        if action == "aa_create":
            assert sum(s["amount_fen"] for s in data["shares"]) == 30001
        if action in {"lost", "restore"}:
            assert data["status"] == ("LOST" if action == "lost" else "ACTIVE")
        if action == "cancel":
            assert data["bank_mandate"] == "REVOKED" and data["merchant_contract"] == "CANCELLED" and data["refund_fen"] == 0
        if action == "overview":
            assert len({n["agent"] for n in done["nodes"]}) == 6
        # Replaying a completed plan must not repeat any financial side effects.
        stable = bank_account(client, headers)
        replay = client.post(f"/api/plans/{done['id']}/resume", headers=headers, json={})
        assert replay.status_code == 200 and bank_account(client, headers) == stable
    assert client.get("/api/audit", headers=headers).json()["chain_verified"]
    with client.app.state.db.read() as session:
        assert session.scalar(select(func.sum(LedgerEntry.delta_fen))) == 0


@pytest.mark.parametrize("kind", ["budget", "budget_transfer", "budget_investment", "budget_transfer_investment"])
def test_allocation_variants_only_execute_selected_businesses(client, headers, kind):
    payroll(client, headers)
    data = {"allocation_kind": kind, "budget_fen": 100000, "purpose": "备用金"}
    if "transfer" in kind:
        data.update(recipient_hint="妈妈", transfer_fen=50000)
    if "investment" in kind:
        data.update(product_id="P_CASH", investment_fen=50000)
    result = complete(client, headers, prepare(client, headers, "life", "allocation", data))
    operations = {n["operation"] for n in result["nodes"]}
    assert ("funds.transfer" in operations) == ("transfer" in kind)
    assert ("wealth.purchase" in operations) == ("investment" in kind)
    assert result["status"] == "COMPLETED"


def test_allocation_rejected_budget_blocks_dependent_payments(client, headers):
    payroll(client, headers)
    plan = prepare(client, headers, "life", "allocation", {"allocation_kind": "budget_transfer_investment", "budget_fen": 100000, "purpose": "备用金", "recipient_hint": "妈妈", "transfer_fen": 50000, "product_id": "P_CASH", "investment_fen": 50000})
    before = bank_account(client, headers)
    node = plan["nodes"][0]
    response = client.post(f"/api/plans/{plan['id']}/approve", headers=headers, json={"node_id": node["id"], "fingerprint": node["fingerprint"], "decision": "reject"})
    assert response.status_code == 200
    assert all(n["status"] == "SKIPPED" for n in response.json()["nodes"][1:])
    assert bank_account(client, headers) == before


def test_preflight_shortfall_and_missing_fields_do_not_create_plans_or_security_failures(client, headers):
    with client.app.state.db.read() as session:
        before = session.scalar(select(func.count()).select_from(Plan))
    for _ in range(3):
        response = client.post("/api/businesses/life/allocation/prepare", headers=headers, json={"params": {
            "allocation_kind": "budget", "budget_fen": 100000, "purpose": "备用金",
        }})
        assert response.status_code == 400 and response.json()["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert not client.get("/api/security/status", headers=headers).json()["locked"]
    assert client.post("/api/businesses/life/allocation/prepare", headers=headers, json={"params": {
        "allocation_kind": "budget_transfer", "budget_fen": 100000, "purpose": "备用金", "recipient_hint": "妈妈",
    }}).status_code == 400
    for extra in [{"recipient_hint": "妈妈"}, {"recipient_hint": "妈妈", "transfer_fen": 50000}, {"product_id": "P_CASH", "investment_fen": 50000}]:
        invalid = client.post("/api/businesses/life/allocation/prepare", headers=headers, json={"params": {
            "allocation_kind": "budget", "budget_fen": 100000, "purpose": "备用金", **extra,
        }})
        assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "INVALID_BUSINESS_ARGUMENTS"
    with client.app.state.db.read() as session:
        assert session.scalar(select(func.count()).select_from(Plan)) == before


def test_records_are_owner_scoped_and_readonly_queries_reject_extra_parameters(client, headers):
    payroll(client, headers)
    waiting = prepare(client, headers, "funds", "transfer", {"recipient_hint": "妈妈", "amount_fen": 50000})
    other = login(client, "wang")
    assert waiting["id"] not in [p["id"] for p in prepare(client, other, "life", "plans")["items"]]
    for category, action in [("life", "plans"), ("funds", "aa_manage")]:
        assert client.post(f"/api/businesses/{category}/{action}/prepare", headers=headers, json={"params": {"user_id": "u_wang"}}).status_code == 400
