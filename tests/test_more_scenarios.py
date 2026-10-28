from sqlalchemy import select

from bank_agent.storage import Document, Grant
from conftest import finish, payroll, start


def test_future_birthday_freezes_only_in_birthday_month(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-11-20"}))
    assert plan["status"] == "WAIT_TIME"
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 0
    client.post("/api/demo/clock", headers=headers, json={"days": 19})
    plan = finish(client, headers, client.get("/api/plans/" + plan["id"], headers=headers).json())
    assert plan["status"] == "WAIT_TIME"
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 100000


def test_early_month_birthday_conflict_requires_clarification(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-11-01"}))
    assert plan["status"] == "WAIT_INPUT"
    assert client.get("/api/overview", headers=headers).json()["account"]["reserved_fen"] == 0


def test_cashflow_time_horizon_and_income_delay_do_not_change_bank_cash(client, headers):
    payroll(client, headers, 10000)
    finish(client, headers, start(client, "cashflow.income_plan", {"date": "2026-10-19", "amount_fen": 600000}))
    short = start(client, "cashflow.project", {"days": 2})["nodes"][0]["result"]["data"]
    assert short["known_future_debits_fen"] == 0
    baseline = start(client, "cashflow.project", {"days": 10})["nodes"][0]["result"]["data"]
    delayed = start(client, "cashflow.project", {"days": 10, "income_delay_days": 10, "expense_increase_pct": 20})[
        "nodes"
    ][0]["result"]["data"]
    assert baseline["indicative_income"] and not delayed["indicative_income"]
    assert delayed["minimum_projected_fen"] == 4240
    assert client.get("/api/overview", headers=headers).json()["account"]["available_fen"] == 10000


def test_wealth_yield_query_uses_nav_and_holding(client, headers):
    payroll(client, headers)
    finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_CASH", "amount_fen": 10000}))
    with client.app.state.db.write() as session:
        product = session.get(Document, "P_CASH")
        product.data = {**product.data, "nav_1e4": 10100}
    holdings = start(client, "wealth.holdings")["nodes"][0]["result"]["data"]["holdings"]
    assert holdings[0]["market_value_fen"] == 10100 and holdings[0]["unrealized_gain_fen"] == 100
    redemption = finish(client, headers, start(client, "wealth.redeem", {"product_id": "P_CASH", "amount_fen": 5000}))
    data = redemption["nodes"][0]["result"]["data"]
    assert data["status"] == "SETTLED" and data["redeemed_cost_basis_fen"] < 5000
    position = start(client, "wealth.holdings")["nodes"][0]["result"]["data"]["holdings"][0]
    assert position["principal_fen"] > 5000


def test_redemption_weekend_uses_next_business_day(client, headers):
    payroll(client, headers)
    finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_STEADY", "amount_fen": 10000}))
    client.post("/api/demo/clock", headers=headers, json={"days": 3})
    plan = finish(client, headers, start(client, "wealth.redeem", {"product_id": "P_STEADY", "amount_fen": 10000}))
    assert plan["nodes"][0]["result"]["data"]["settles_on"] == "2026-10-19"


def test_expired_grant_needs_confirmation_again(client, headers):
    from bank_agent.schemas import ApprovalRequest

    payroll(client, headers)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000})
    node = plan["nodes"][0]
    service = client.app.state.plans
    service.authorize("u_demo", plan["id"], ApprovalRequest(node_id=node["id"], fingerprint=node["fingerprint"]))
    with client.app.state.db.write() as session:
        grant = session.scalar(select(Grant).where(Grant.node_id == node["id"]))
        grant.expires_at = "2000-01-01T00:00:00+00:00"
    assert service.step("u_demo", plan["id"]) == "WAIT_CONFIRM"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def test_repeated_insufficient_funds_does_not_open_security_circuit(client, headers):
    plan = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000}))
    for _ in range(2):
        plan = client.post("/api/plans/" + plan["id"] + "/resume", headers=headers).json()
        plan = finish(client, headers, plan)
    assert not client.get("/api/auth/me", headers=headers).json()["locked"]
    assert not any(event["kind"] == "CIRCUIT_OPEN" for event in client.get("/api/audit", headers=headers).json()["events"])


def test_physical_card_and_bank_limit_decline(client, headers):
    plan = finish(client, headers, start(client, "cards.apply", {"kind": "physical"}))
    assert plan["nodes"][0]["tier"] == "RED"
    assert plan["nodes"][0]["result"]["data"]["status"] == "APPROVED_PENDING_DELIVERY"
    plan = finish(client, headers, start(client, "cards.limit", {"amount_fen": 1300000}))
    assert plan["nodes"][0]["error"]["code"] == "LIMIT_DECLINED"
