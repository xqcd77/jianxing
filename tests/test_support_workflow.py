from datetime import datetime, timedelta, timezone
from sqlalchemy import select
import pytest

from bank_agent.storage import Document, Plan, Receipt, User
from conftest import finish, login, payroll, start


def unsupported(client, headers, scope="both"):
    with client.app.state.db.write() as s:
        doc = s.get(Document, "S_VIDEO")
        doc.data = {**doc.data, "cancellation_supported": False}
    p = finish(client, headers, start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO", "scope": scope}))
    assert len(p["support_cases"]) == 1
    return p, p["support_cases"][0]


def test_supported_video_cancel_is_automatic_after_confirmation(client, headers):
    p = finish(client, headers, start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO"}))
    assert p["status"] == "COMPLETED" and p["support_cases"] == []
    assert p["nodes"][0]["result"]["data"]["merchant_contract"] == "CANCELLED"


def test_security_lock_is_self_service_not_human_support(client, headers):
    with client.app.state.db.write() as s:
        s.get(User, "u_demo").locked = True
    p = start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO"})
    assert p["status"] == "WAIT_INPUT" and p["support_cases"] == []
    assert "无需转人工" in p["assistant_message"]


def test_unsupported_bank_only_never_needs_merchant_support(client, headers):
    with client.app.state.db.write() as s:
        doc = s.get(Document, "S_VIDEO")
        doc.data = {**doc.data, "cancellation_supported": False}
    p = finish(client, headers, start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO", "scope": "bank"}))
    assert p["status"] == "COMPLETED" and not p["support_cases"]
    assert p["nodes"][0]["result"]["data"]["merchant_contract"] == "ACTIVE"


def test_partial_cancel_has_review_followup_reconfirmation_and_final_receipt(client, headers):
    original, case = unsupported(client, headers)
    assert original["status"] == "PARTIAL"
    base = "/api/support/cases/" + case["id"]
    reviewed = client.post(base + "/demo-review", headers=headers).json()
    assert reviewed["status"] == "ACTION_REQUIRED"
    with client.app.state.db.read() as s:
        assert s.get(Document, "S_VIDEO").data["merchant_contract"] == "ACTIVE"
    child = client.post(base + "/continue", headers=headers).json()
    assert child["status"] == "WAIT_CONFIRM" and child["nodes"][0]["params"]["scope"] == "merchant"
    assert client.post(base + "/continue", headers=headers).json()["id"] == child["id"]
    done = finish(client, headers, child)
    assert done["status"] == "COMPLETED"
    assert client.get(base, headers=headers).json()["status"] == "RESOLVED"
    assert client.post(base + "/continue", headers=headers).json()["id"] == child["id"]
    with client.app.state.db.read() as s:
        c = s.get(Document, "S_VIDEO").data
        assert c["merchant_contract"] == "CANCELLED" and c["bank_mandate"] == "REVOKED"
        receipts = list(s.scalars(select(Receipt).where(Receipt.operation == "subscriptions.cancel")))
        assert len(receipts) == 2
        assert s.get(Plan, original["id"]).status == "PARTIAL"


def test_merchant_only_assistance_does_not_revoke_bank_mandate(client, headers):
    p, c = unsupported(client, headers, "merchant")
    assert p["status"] == "WAIT_HUMAN"
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    child = client.post(root + "/continue", headers=headers).json()
    assert finish(client, headers, child)["status"] == "COMPLETED"
    with client.app.state.db.read() as s:
        assert s.get(Document, "S_VIDEO").data["bank_mandate"] == "ACTIVE"


def test_review_cannot_be_forged_or_reused_for_another_contract(client, headers):
    p = start(
        client,
        "subscriptions.cancel",
        {"subscription_id": "S_VIDEO", "assistance_case_id": "forged", "assistance_version": 1},
    )
    assert p["status"] == "FAILED"
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    p = start(
        client,
        "subscriptions.cancel",
        {"subscription_id": "S_MUSIC", "scope": "merchant", "assistance_case_id": c["id"], "assistance_version": 1},
    )
    assert p["status"] == "FAILED" and p["nodes"][0]["error"]["code"] == "SUPPORT_SCOPE_MISMATCH"


def test_contract_change_or_expired_review_requires_another_review(client, headers):
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    with client.app.state.db.write() as s:
        d = s.get(Document, "S_VIDEO")
        d.data = {**d.data, "amount_fen": 3900}
    assert client.post(root + "/continue", headers=headers).status_code == 409
    client.post(root + "/demo-review", headers=headers)
    with client.app.state.db.write() as s:
        d = s.get(Document, c["id"])
        d.data = {**d.data, "proposal_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    assert client.post(root + "/continue", headers=headers).status_code == 409


def test_support_is_owned_and_closing_never_cancels_contract(client, headers):
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    other = login(client, "wang")
    assert client.get(root, headers=other).status_code == 404
    assert client.post(root + "/demo-review", headers=other).status_code == 404
    assert client.post(root + "/messages", headers=headers, json={"text": "请协助核实会员取消范围"}).status_code == 200
    assert client.post(root + "/close", headers=headers).json()["status"] == "CLOSED"
    assert client.post(root + "/continue", headers=headers).status_code == 409
    with client.app.state.db.read() as s:
        assert s.get(Document, "S_VIDEO").data["merchant_contract"] == "ACTIVE"


def test_user_requested_handoff_is_deduplicated_and_keeps_original_authorization(client, headers):
    payroll(client, headers)
    p = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000})
    root = "/api/plans/" + p["id"] + "/handoff"
    p = client.post(root, headers=headers).json()
    again = client.post(root, headers=headers).json()
    assert p["support_cases"][0]["id"] == again["support_cases"][0]["id"]
    case = "/api/support/cases/" + p["support_cases"][0]["id"]
    client.post(case + "/demo-review", headers=headers)
    follow = client.post(case + "/continue", headers=headers).json()
    assert follow["status"] == "WAIT_CONFIRM"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
    assert finish(client, headers, follow)["status"] == "COMPLETED"
    assert client.get(case, headers=headers).json()["status"] == "RESOLVED"


def test_human_support_never_replays_an_unknown_submission(client, headers):
    from bank_agent.adapters import LostResponseSandboxAdapter

    client.app.state.plans.adapter = LostResponseSandboxAdapter(client.app.state.bank)
    payroll(client, headers)
    p = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000}))
    assert p["status"] == "UNKNOWN"
    p = client.post("/api/plans/" + p["id"] + "/handoff", headers=headers).json()
    assert p["status"] == "UNKNOWN"
    root = "/api/support/cases/" + p["support_cases"][0]["id"]
    client.post(root + "/demo-review", headers=headers)
    result = client.post(root + "/continue", headers=headers).json()
    assert result["status"] == "COMPLETED"
    with client.app.state.db.read() as s:
        assert len(list(s.scalars(select(Receipt).where(Receipt.operation == "funds.transfer")))) == 1


def test_close_case_blocks_previously_created_assisted_plan(client, headers):
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    p = client.post(root + "/continue", headers=headers).json()
    client.post(root + "/close", headers=headers)
    from conftest import approve

    r = approve(client, headers, p)
    assert r.status_code == 409
    with client.app.state.db.read() as s:
        assert s.get(Document, "S_VIDEO").data["merchant_contract"] == "ACTIVE"


def test_technical_review_requires_real_local_healthcheck_and_reconfirmation(client, headers):
    from bank_agent.storage import save_document

    payroll(client, headers)
    with client.app.state.db.write() as s:
        save_document(s, "circuit_u_demo", "circuit", "u_demo", {"failures": 3, "open": True})
    p = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000})
    assert p["status"] == "WAIT_HUMAN"
    root = "/api/support/cases/" + p["support_cases"][0]["id"]
    client.post(root + "/demo-review", headers=headers)
    resumed = client.post(root + "/continue", headers=headers).json()
    assert resumed["status"] == "WAIT_CONFIRM"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def test_demo_review_is_not_a_normal_user_capability(client, headers):
    from bank_agent.support import ensure_case

    other = login(client, "wang")
    p = start(client, "balance.query", user_id="u_wang")
    with client.app.state.db.write() as s:
        plan = s.get(Plan, p["id"])
        c = ensure_case(s, "u_wang", plan)
    assert client.post("/api/support/cases/" + c["id"] + "/demo-review", headers=other).status_code == 403


def test_cancelled_contract_does_not_get_another_unsupported_merchant_case(client, headers):
    with client.app.state.db.write() as s:
        c = s.get(Document, "S_VIDEO")
        c.data = {**c.data, "merchant_contract": "CANCELLED", "cancellation_supported": False}
    p = finish(
        client, headers, start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO", "scope": "merchant"})
    )
    assert p["status"] == "COMPLETED" and p["support_cases"] == []


def test_closed_case_can_be_reopened_without_reusing_old_review(client, headers):
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    old = client.post(root + "/continue", headers=headers).json()
    client.post(root + "/close", headers=headers)
    assert client.post(root + "/reopen", headers=headers).json()["status"] == "PENDING"
    client.post(root + "/demo-review", headers=headers)
    new = client.post(root + "/continue", headers=headers).json()
    assert new["id"] != old["id"]
    from conftest import approve

    assert approve(client, headers, old).status_code == 409
    assert finish(client, headers, new)["status"] == "COMPLETED"


def test_support_does_not_replace_large_transfer_mfa(client, headers):
    payroll(client, headers)
    p = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 200000})
    p = client.post("/api/plans/" + p["id"] + "/handoff", headers=headers).json()
    root = "/api/support/cases/" + p["support_cases"][0]["id"]
    client.post(root + "/demo-review", headers=headers)
    p = client.post(root + "/continue", headers=headers).json()
    assert p["status"] == "WAIT_MFA"
    node = p["nodes"][0]
    denied = client.post(
        "/api/plans/" + p["id"] + "/approve",
        headers=headers,
        json={"node_id": node["id"], "fingerprint": node["fingerprint"]},
    )
    assert denied.status_code == 401
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def test_legacy_security_wait_migration_keeps_account_locked(client, headers):
    from bank_agent.migrations import migrate_support_boundaries
    from bank_agent.storage import Node

    p = start(client, "subscriptions.cancel", {"subscription_id": "S_VIDEO"})
    with client.app.state.db.write() as s:
        s.get(User, "u_demo").locked = True
        n = s.get(Node, p["nodes"][0]["id"])
        n.status = "WAIT_HUMAN"
        n.error = {"code": "SECURITY_LOCKED", "message": "旧版安全锁定"}
        s.get(Plan, p["id"]).status = "WAIT_HUMAN"
        s.delete(s.get(Document, "migration_support_boundaries_v2"))
    migrate_support_boundaries(client.app.state.db)
    fixed = client.get("/api/plans/" + p["id"], headers=headers).json()
    assert fixed["status"] == "WAIT_INPUT" and fixed["security_locked"] and fixed["support_cases"] == []


@pytest.mark.parametrize("change", ["expired", "contract_changed"])
def test_review_can_be_updated_after_followup_is_waiting_for_confirmation(client, headers, change):
    _, c = unsupported(client, headers)
    root = "/api/support/cases/" + c["id"]
    client.post(root + "/demo-review", headers=headers)
    old = client.post(root + "/continue", headers=headers).json()
    with client.app.state.db.write() as s:
        if change == "expired":
            d = s.get(Document, c["id"])
            d.data = {**d.data, "proposal_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        else:
            d = s.get(Document, "S_VIDEO")
            d.data = {**d.data, "amount_fen": 4900}
    assert client.post(root + "/demo-review", headers=headers).json()["status"] == "ACTION_REQUIRED"
    new = client.post(root + "/continue", headers=headers).json()
    assert new["id"] != old["id"]
    from conftest import approve

    assert approve(client, headers, old).status_code == 409
    assert finish(client, headers, new)["status"] == "COMPLETED"
