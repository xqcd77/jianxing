from datetime import timedelta

from fastapi.testclient import TestClient
import pyotp
import pytest

from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.schemas import Intent
from bank_agent.seed import DEMO_PASSWORD
from bank_agent.storage import User


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode="demo"))
    with TestClient(app) as result:
        yield result


def login(client, username="demo"):
    result = client.post("/api/auth/login", json={"username": username, "password": DEMO_PASSWORD})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["access_token"]}


@pytest.fixture
def headers(client):
    return login(client)


def payroll(client, headers, amount=600000, event="fixture-salary"):
    result = client.post("/api/demo/payroll", headers=headers, json={"event_id": event, "amount_fen": amount})
    assert result.status_code == 200, result.text
    return result.json()


def start(client, op, params=None, user_id="u_demo", scheduled_for=None):
    intent = Intent(
        summary=op,
        tasks=[
            {
                "domain": __import__("bank_agent.operations", fromlist=["DEFINITIONS"]).DEFINITIONS[op][0],
                "action": op,
                "params": params or {},
                "scheduled_for": scheduled_for,
            }
        ],
    )
    return client.app.state.graph.start(user_id, op, intent=intent)


def new_otp(client, user_id="u_demo"):
    auth = client.app.state.auth
    # Test clock injection stays inside the fixture, never exposed as an API.
    current = auth.clock()
    auth.clock = lambda: current + timedelta(seconds=31)
    with client.app.state.db.read() as session:
        user = session.get(User, user_id)
        secret = auth.fernet.decrypt(user.totp_encrypted.encode()).decode()
    return pyotp.TOTP(secret).at(auth.clock())


def approve(client, headers, plan, node=None, user_id="u_demo", otp=None, decision="approve"):
    if node is None:
        node = next(n for n in plan["nodes"] if n["status"] in ["WAIT_CONFIRM", "WAIT_MFA"])
    data = {"node_id": node["id"], "fingerprint": node["fingerprint"], "decision": decision}
    if node["tier"] == "RED" and decision == "approve":
        data["otp"] = otp if otp is not None else new_otp(client, user_id)
    return client.post("/api/plans/" + plan["id"] + "/approve", headers=headers, json=data)


def finish(client, headers, plan, user_id="u_demo"):
    for _ in range(40):
        pending = next((n for n in plan["nodes"] if n["status"] in ["WAIT_CONFIRM", "WAIT_MFA"]), None)
        if not pending:
            return plan
        response = approve(client, headers, plan, pending, user_id)
        assert response.status_code == 200, response.text
        plan = response.json()
    raise AssertionError("plan did not converge")
