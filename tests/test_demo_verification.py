from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from bank_agent.storage import Document, User
from conftest import approve, login, new_otp, payroll, start


def issue(client, headers, purpose="unlock", plan=None, node=None):
    payload = {"purpose": purpose}
    if plan:
        node = node or plan["nodes"][0]
        payload.update(plan_id=plan["id"], node_id=node["id"], fingerprint=node["fingerprint"])
    response = client.post("/api/security/demo-code", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def credential(code):
    return {"otp": code["code"], "demo_challenge_id": code["challenge_id"]}


def verify_approval(client, headers, plan, code, node=None):
    node = node or plan["nodes"][0]
    return client.post(f"/api/plans/{plan['id']}/approve", headers=headers, json={
        "node_id": node["id"], "fingerprint": node["fingerprint"], **credential(code),
    })


def lock_account(client):
    with client.app.state.db.write() as session:
        user = session.get(User, "u_demo")
        user.locked = True
        user.failed_verifications = 3


def test_locked_incoming_unlock_and_red_transfer_closed_loop(client, headers):
    lock_account(client)
    status = client.get("/api/security/status", headers=headers).json()
    assert status["locked"] and status["demo_verification"] and "验证码" in status["reason"]
    request = client.post("/api/chat", headers=headers, json={"message": "现在我想转入10000元"}).json()
    if request["status"] == "NEEDS_CLARIFICATION":
        request = client.post("/api/chat", headers=headers, json={
            "message": "是我的账户", "conversation_id": request["conversation_id"],
        }).json()
    assert request["status"] == "WAIT_DEMO_CONFIRM", request
    path = f"/api/demo/incoming/{request['ui_action']['request_id']}/decision"
    assert client.post(path, headers=headers, json={"decision": "confirm"}).status_code == 423
    code = issue(client, headers)
    assert client.get("/api/auth/me", headers=headers).json()["locked"]  # Issuing does not unlock.
    assert client.post("/api/security/unlock", headers=headers, json=credential(code)).status_code == 200
    assert not client.get("/api/security/status", headers=headers).json()["locked"]
    assert client.post(path, headers=headers, json={"decision": "confirm"}).status_code == 200
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 1_000_000
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 200_000})
    assert plan["status"] == "WAIT_MFA"
    code = issue(client, headers, "approve", plan)
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 1_000_000
    response = verify_approval(client, headers, plan, code)
    assert response.status_code == 200 and response.json()["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 800_000


def test_demo_code_replay_and_atomic_concurrent_consumption(client, headers):
    code = issue(client, headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/api/security/unlock", headers=headers, json=credential(code)), range(2)))
    assert sorted(r.status_code for r in results) == [200, 401]
    with client.app.state.db.read() as session:
        assert session.get(User, "u_demo").failed_verifications == 1


def test_expiry_and_replacement(client, headers):
    old = issue(client, headers)
    new = issue(client, headers)
    assert old["challenge_id"] != new["challenge_id"]
    assert client.post("/api/security/unlock", headers=headers, json=credential(old)).status_code == 401
    assert client.post("/api/security/unlock", headers=headers, json=credential(new)).status_code == 200
    expired = issue(client, headers)
    now = client.app.state.auth.clock()
    client.app.state.auth.clock = lambda: now + timedelta(seconds=121)
    assert client.post("/api/security/unlock", headers=headers, json=credential(expired)).status_code == 401


def test_three_invalid_codes_lock_then_new_demo_code_unlocks(client, headers):
    code = issue(client, headers)
    wrong = (int(code["code"]) + 1) % 1_000_000
    for _ in range(3):
        bad = {**credential(code), "otp": f"{wrong:06d}"}
        assert client.post("/api/security/unlock", headers=headers, json=bad).status_code == 401
    assert client.get("/api/auth/me", headers=headers).json()["locked"]
    fresh = issue(client, headers)
    assert client.post("/api/security/unlock", headers=headers, json=credential(fresh)).status_code == 200


def test_code_cannot_cross_users_or_purposes(client, headers):
    code = issue(client, headers)
    other = login(client, "wang")
    assert client.post("/api/security/unlock", headers=other, json=credential(code)).status_code == 401
    payroll(client, headers)
    plan = start(client, "wealth.purchase", {"product_id": "P_CASH", "amount_fen": 10000})
    assert verify_approval(client, headers, plan, code).status_code == 401
    assert client.post("/api/security/unlock", headers=headers, json=credential(code)).status_code == 200


def test_code_bound_to_node_and_plan_version(client, headers):
    payroll(client, headers)
    a = start(client, "wealth.purchase", {"product_id": "P_CASH", "amount_fen": 10000})
    b = start(client, "wealth.purchase", {"product_id": "P_CASH", "amount_fen": 20000})
    a_code = issue(client, headers, "approve", a)
    assert verify_approval(client, headers, b, a_code).status_code == 401
    b_code = issue(client, headers, "approve", b)
    node = a["nodes"][0]
    response = client.post(f"/api/plans/{a['id']}/edit", headers=headers, json={
        "node_id": node["id"], "params": {**node["params"], "amount_fen": 30000},
    })
    assert response.status_code == 200
    revised = response.json()
    assert revised["version"] > a["version"]
    assert verify_approval(client, headers, revised, a_code).status_code == 401
    # Codes for other unchanged plans remain valid.
    assert verify_approval(client, headers, b, b_code).status_code == 200
    fresh = issue(client, headers, "approve", revised)
    assert verify_approval(client, headers, revised, fresh).status_code == 200


def test_issue_checks_auth_owner_pending_and_fingerprint(client, headers):
    assert client.post("/api/security/demo-code", json={"purpose": "unlock"}).status_code == 401
    payroll(client, headers)
    plan = start(client, "wealth.purchase", {"product_id": "P_CASH", "amount_fen": 10000})
    payload = {"purpose": "approve", "plan_id": plan["id"], "node_id": plan["nodes"][0]["id"], "fingerprint": plan["nodes"][0]["fingerprint"]}
    assert client.post("/api/security/demo-code", headers=login(client, "wang"), json=payload).status_code == 404
    assert client.post("/api/security/demo-code", headers=headers, json={**payload, "fingerprint": "0" * 64}).status_code == 409
    assert approve(client, headers, plan, otp=new_otp(client)).status_code == 200
    assert client.post("/api/security/demo-code", headers=headers, json=payload).status_code == 409


def test_locked_account_only_issues_unlock_challenge(client, headers):
    lock_account(client)
    assert client.post("/api/security/demo-code", headers=headers, json={"purpose": "password"}).status_code == 423
    assert issue(client, headers)["verification_mode"] == "demo"


def test_disabled_demo_rejects_issuance_and_existing_code_but_preserves_totp(client, headers):
    for field in ["demo_enabled", "demo_verification_enabled"]:
        code = issue(client, headers)
        saved = client.app.state.auth.settings
        client.app.state.auth.settings = replace(saved, **{field: False})
        assert not client.get("/api/security/status", headers=headers).json()["demo_verification"]
        assert client.post("/api/security/demo-code", headers=headers, json={"purpose": "unlock"}).status_code == 403
        assert client.post("/api/security/unlock", headers=headers, json=credential(code)).status_code == 403
        assert client.post("/api/security/unlock", headers=headers, json={"otp": new_otp(client)}).status_code == 200
        client.app.state.auth.settings = saved


def test_demo_password_change_revokes_sessions_and_still_requires_old_password(client, headers):
    from bank_agent.seed import DEMO_PASSWORD

    code = issue(client, headers, "password")
    payload = {"old_password": "wrong", "new_password": "ChangedBank!2026", **credential(code)}
    assert client.post("/api/security/password", headers=headers, json=payload).status_code == 401
    payload["old_password"] = DEMO_PASSWORD
    assert client.post("/api/security/password", headers=headers, json=payload).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert client.post("/api/auth/login", json={"username": "demo", "password": payload["new_password"]}).status_code == 200


def test_demo_credentials_not_logged_or_stored_in_clear(client, headers):
    code = issue(client, headers)
    assert client.post("/api/security/unlock", headers=headers, json=credential(code)).status_code == 200
    with client.app.state.db.read() as session:
        document = session.get(Document, "demo_verification_u_demo_unlock_account")
        assert "code" not in document.data and document.data["code_hash"] != code["code"]
    events = client.get("/api/audit", headers=headers).json()
    verified = next(e for e in reversed(events["events"]) if e["kind"] == "MFA_VERIFIED")
    assert verified["details"]["factor"] == "demo_code" and verified["details"]["simulated"]
    assert code["code"] not in str(events)
    assert events["chain_verified"]
