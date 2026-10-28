"""Consistency between product comparison, suitability and current funds."""
from conftest import payroll, start
from bank_agent.storage import User


def test_expired_assessment_is_not_presented_as_purchase_eligible(client, headers):
    payroll(client, headers)
    with client.app.state.db.write() as session:
        session.get(User, "u_demo").risk_valid_until = "2026-10-12"
    plan = start(client, "wealth.compare")
    result = plan["nodes"][0]["result"]["data"]
    assert result["recommended_product_ids"] == []
    assert all(not p["eligible"] for p in result["products"])
    assert all(any("测评已过期" in reason for reason in p["eligibility_reasons"]) for p in result["products"])


def test_insufficient_funds_do_not_look_like_a_risk_mismatch(client):
    plan = start(client, "wealth.compare")
    result = plan["nodes"][0]["result"]["data"]
    cash = next(p for p in result["products"] if p["id"] == "P_CASH")
    assert cash["risk_matched"] is True and cash["eligible"] is False
    assert cash["eligibility_reasons"] == ["当前可用资金未达到起购金额"]
