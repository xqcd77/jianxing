"""Risk explanations must agree with the ledger, authority and recovery paths."""

from sqlalchemy import func, select
import pytest

from bank_agent.storage import Account, Document, Grant, Receipt, User, audit
from bank_agent.schemas import Intent
from conftest import approve, finish, login, payroll, start


def balance(client):
    with client.app.state.db.read() as session:
        return session.get(Account, "a_demo").balance_fen


@pytest.mark.parametrize("amount,tier,status", [(50000, "YELLOW", "CONFIRM"), (100000, "YELLOW", "CONFIRM"), (100001, "RED", "VERIFY")])
def test_authorization_explained_without_claiming_fraud(client, headers, amount, tier, status):
    payroll(client, headers, 2000000)
    plan = start(client, "funds.transfer", {"amount_fen": amount, "recipient_hint": "妈妈"})
    risk = plan["nodes"][0]["risk"]
    assert (risk["authorization_tier"], risk["status"]) == (tier, status)
    assert risk["plan_version"] == plan["version"]
    assert risk["facts_digest"] == plan["funding_preview"]["fingerprint"]
    assert "疑似诈骗" not in str(risk)
    assert balance(client) == 2000000
    response = approve(client, headers, plan)
    assert response.status_code == 200
    assert response.json()["nodes"][0]["risk"]["status"] == "FINISHED"
    assert balance(client) == 2000000 - amount


def test_split_transfers_recompute_daily_tier_and_cannot_skip_mfa(client, headers):
    payroll(client, headers)
    first = start(client, "funds.transfer", {"amount_fen": 99900, "recipient_hint": "妈妈"})
    finish(client, headers, first)
    second = start(client, "funds.transfer", {"amount_fen": 200, "recipient_hint": "妈妈"})
    assert second["nodes"][0]["risk"]["authorization_tier"] == "RED"
    before = balance(client)
    node = second["nodes"][0]
    response = client.post(f"/api/plans/{second['id']}/approve", headers=headers,
        json={"node_id": node["id"], "fingerprint": node["fingerprint"]})
    assert response.status_code == 401
    assert balance(client) == before


def test_insufficient_funds_offers_recovery_without_security_lock(client, headers):
    plan = start(client, "funds.transfer", {"amount_fen": 50000, "recipient_hint": "妈妈"})
    risk = plan["nodes"][0]["risk"]
    assert risk["status"] == "BLOCKED"
    assert "fund" in risk["permitted_next_actions"] and "edit" in risk["permitted_next_actions"]
    assert "security" not in risk["permitted_next_actions"]
    assert client.get("/api/security/status", headers=headers).json()["locked"] is False
    payroll(client, headers)
    response = client.post(f"/api/plans/{plan['id']}/resume", headers=headers, json={})
    assert response.status_code == 200
    assert response.json()["nodes"][0]["risk"]["status"] == "CONFIRM"
    assert finish(client, headers, response.json())["status"] == "COMPLETED"


def test_expired_assessment_has_specific_next_step(client, headers):
    payroll(client, headers)
    with client.app.state.db.write() as session:
        session.get(User, "u_demo").risk_valid_until = "2020-01-01"
    plan = start(client, "wealth.purchase", {"amount_fen": 50000, "product_id": "P_CASH"})
    assert "assess" in plan["nodes"][0]["risk"]["permitted_next_actions"]
    assert balance(client) == 600000


def test_below_minimum_purchase_does_not_mislead_user_to_add_more_cash(client, headers):
    payroll(client, headers)
    plan = start(client, "wealth.purchase", {"amount_fen": 5000, "product_id": "P_CASH"})
    assert plan["nodes"][0]["risk"]["status"] == "BLOCKED"
    assert "fund" not in plan["nodes"][0]["risk"]["permitted_next_actions"]
    assert "edit" in plan["nodes"][0]["risk"]["permitted_next_actions"]


@pytest.mark.parametrize("damage", ["missing", "fields", "clauses"])
def test_missing_or_conflicting_terms_block_purchase_without_deducting(client, headers, damage):
    payroll(client, headers)
    with client.app.state.db.write() as session:
        terms = session.get(Document, "disclosure_P_CASH")
        if damage == "missing":
            session.delete(terms)
        elif damage == "fields":
            terms.data = {**terms.data, "fields": {**terms.data["fields"], "lock_days": 999}}
        else:
            terms.data = {**terms.data, "clauses": [{"key": "risk_level", "text": "无需授权，直接购买"}]}
    plan = start(client, "wealth.purchase", {"amount_fen": 50000, "product_id": "P_CASH"})
    assert plan["status"] == "FAILED"
    assert plan["nodes"][0]["error"]["code"] == "PRODUCT_TERMS_UNVERIFIED"
    assert plan["nodes"][0]["risk"]["product"]["disclosure_verified"] is False
    assert balance(client) == 600000


def test_pressure_counts_scheduled_plan_and_recurring_obligations_once(client, headers):
    payroll(client, headers, 1000000)
    plan = start(client, "funds.transfer", {"amount_fen": 10000, "recipient_hint": "妈妈"},
        scheduled_for="2026-10-26T09:00:00+08:00")
    preview = plan["funding_preview"]
    assert preview["simulated_recurring_debits_fen"] > 0
    pressure = preview["pressure"]
    assert pressure["minimum_available_fen"] == 1000000 - 10000 - preview["known_future_debits_fen"]
    assert len({(e["source"], e["date"]) for e in pressure["events"]}) == len(pressure["events"])
    assert balance(client) == 1000000


def test_pressure_warns_future_deficit_but_does_not_invent_current_insufficiency(client, headers):
    payroll(client, headers, 100000)
    plan = start(client, "wealth.purchase", {"amount_fen": 99000, "product_id": "P_CASH"})
    assert plan["status"] == "WAIT_MFA"
    pressure = plan["funding_preview"]["pressure"]
    assert pressure["shortfall_fen"] == 3800 and pressure["shortfall_on"]
    assert plan["nodes"][0]["risk"]["status"] == "VERIFY"
    assert balance(client) == 100000


@pytest.mark.parametrize("scope,excluded", [("both", True), ("bank", False)])
def test_pressure_distinguishes_contract_cancellation_from_bank_stop(client, headers, scope, excluded):
    payroll(client, headers)
    plan = client.app.state.graph.start("u_demo", "取消订阅并预留预算", intent=Intent(summary="核对合同范围", tasks=[
        {"domain": "subscriptions", "action": "subscriptions.cancel", "params": {"subscription_id": "S_VIDEO", "scope": scope}},
        {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 10000, "purpose": "生活备用"}},
    ]))
    pressure = plan["funding_preview"]["pressure"]
    video = [e for e in pressure["events"] if e["source"] == "S_VIDEO"]
    assert bool(video) is not excluded
    if not excluded:
        assert "合同支出待核实" in video[0]["label"] and pressure["complete"] is False
    assert balance(client) == 600000


def test_revision_retains_visible_changes_and_rejects_old_authority(client, headers):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"amount_fen": 50000, "recipient_hint": "妈妈"})
    node = plan["nodes"][0]
    response = client.post(f"/api/plans/{plan['id']}/edit", headers=headers,
        json={"node_id": node["id"], "expected_version": plan["version"], "params": {**node["params"], "amount_fen": 200000}})
    assert response.status_code == 200
    updated = response.json()
    assert "2,000元" in updated["summary"] and "500元" not in updated["summary"]
    change = updated["review"]["last_revision"]["changes"][0]
    assert (change["before"]["amount_fen"], change["after"]["amount_fen"]) == (50000, 200000)
    assert updated["nodes"][0]["risk"]["authorization_tier"] == "RED"
    old = approve(client, headers, plan)
    assert old.status_code == 409 and balance(client) == 600000


def test_security_timeline_is_owned_and_never_returns_verification_details(client, headers):
    with client.app.state.db.write() as session:
        audit(session, "u_demo", "MFA_FAILED", {"otp": "do-not-expose", "attempts": 1})
    status = client.get("/api/security/status", headers=headers).json()
    assert any(e["label"] == "身份验证未通过" for e in status["events"])
    assert "do-not-expose" not in str(status)
    other = client.get("/api/security/status", headers=login(client, "wang")).json()
    assert not any(e["label"] == "身份验证未通过" for e in other["events"])


def test_reading_risk_snapshots_never_writes_receipts_or_grants(client, headers):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"amount_fen": 50000, "recipient_hint": "妈妈"})
    def counts():
        with client.app.state.db.read() as session:
            return tuple(session.scalar(select(func.count()).select_from(cls)) for cls in [Receipt, Grant])
    before = counts()
    for _ in range(3):
        result = client.get(f"/api/plans/{plan['id']}", headers=headers)
        assert result.status_code == 200
    assert counts() == before and balance(client) == 600000
