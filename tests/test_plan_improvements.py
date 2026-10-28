from pathlib import Path
import multiprocessing
import time

from sqlalchemy import func, select

from bank_agent.adapters import LookupResult, LostResponseSandboxAdapter, SandboxBankAdapter, SubmissionUncertain
from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.errors import BankError
from bank_agent.locking import ProcessLocks
from bank_agent.schemas import ApprovalRequest, Intent
from bank_agent.storage import Account, Document, Grant, LedgerEntry, Receipt, User
from conftest import approve, finish, login, payroll, start


def mixed_plan(client):
    return client.app.state.graph.start("u_demo", "预留3000元生活费，给妈妈转800元，申购模拟现金产品500元", intent=Intent(
        summary="组合办理", tasks=[
            {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 300000, "purpose": "生活费"}},
            {"domain": "funds", "action": "funds.transfer", "params": {"amount_fen": 80000, "recipient_hint": "妈妈"}},
            {"domain": "wealth", "action": "wealth.purchase", "params": {"product_id": "P_CASH", "amount_fen": 50000}},
        ]))


def batch(client, headers, plan):
    return client.post(f"/api/plans/{plan['id']}/approve-batch", headers=headers,
        json={"expected_version": plan["version"], "manifest_fingerprint": plan["confirmation_batch"]["fingerprint"]})


def test_whole_plan_preview_is_exact_and_does_not_commit(client, headers):
    payroll(client, headers)
    plan = mixed_plan(client)
    p = plan["funding_preview"]
    assert p["required_fen"] == 430000
    assert p["after"] == {"balance_fen": 470000, "reserved_fen": 300000, "available_fen": 170000}
    assert p["known_future_debits_fen"] == 4800 and p["ready"]
    with client.app.state.db.read() as s:
        assert s.get(Account, "a_demo").balance_fen == 600000
        assert s.scalar(select(func.count()).select_from(Grant)) == 0
        assert s.scalar(select(func.count()).select_from(Receipt)) == 1  # Only payroll.
        assert not any(d.data.get("status") == "ACTIVE" for d in s.scalars(select(Document).where(Document.kind == "hold")))
    for _ in range(3):
        reread = client.get(f"/api/plans/{plan['id']}", headers=headers).json()
        assert reread["confirmation_batch"]["fingerprint"] == plan["confirmation_batch"]["fingerprint"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def test_yellow_batch_then_independent_red_mfa(client, headers):
    payroll(client, headers)
    plan = mixed_plan(client)
    assert plan["confirmation_batch"]["available"] and len(plan["confirmation_batch"]["items"]) == 2
    response = batch(client, headers, plan)
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["status"] == "WAIT_MFA"
    assert [n["status"] for n in changed["nodes"]] == ["SUCCEEDED", "SUCCEEDED", "WAIT_MFA"]
    red = changed["nodes"][2]
    refused = client.post(f"/api/plans/{plan['id']}/approve", headers=headers,
                         json={"node_id": red["id"], "fingerprint": red["fingerprint"]})
    assert refused.status_code == 401
    final = finish(client, headers, changed)
    assert final["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"] == {"id": "a_demo", "balance_fen": 470000,
        "reserved_fen": 300000, "available_fen": 170000}


def test_batch_accounts_for_cumulative_transfer_risk(client, headers):
    payroll(client, headers)
    plan = client.app.state.graph.start("u_demo", "组合转账", intent=Intent(summary="组合转账", tasks=[
        {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 10000, "purpose": "备用"}},
        {"domain": "funds", "action": "funds.transfer", "params": {"amount_fen": 80000, "recipient_hint": "妈妈"}},
        {"domain": "funds", "action": "funds.transfer", "params": {"amount_fen": 30000, "recipient_hint": "小王"}},
    ]))
    assert [s["tier"] for s in plan["funding_preview"]["steps"]] == ["YELLOW", "YELLOW", "RED"]
    assert len(plan["confirmation_batch"]["items"]) == 2
    result = batch(client, headers, plan).json()
    assert result["nodes"][2]["tier"] == "RED" and result["nodes"][2]["status"] == "WAIT_MFA"


def test_stale_manifest_version_money_and_ownership_are_rejected(client, headers):
    payroll(client, headers)
    plan = mixed_plan(client)
    assert batch(client, login(client, "wang"), plan).status_code == 404
    payroll(client, headers, 10000, event="changed-state")
    assert batch(client, headers, plan).json()["error"]["code"] == "STALE_BATCH"
    n = plan["nodes"][0]
    revised = client.post(f"/api/plans/{plan['id']}/edit", headers=headers, json={"node_id": n["id"],
        "params": {**n["params"], "amount_fen": 200000}, "expected_version": plan["version"]}).json()
    assert revised["version"] == 2
    assert batch(client, headers, plan).status_code == 409
    stale = client.post(f"/api/plans/{plan['id']}/edit", headers=headers, json={"node_id": n["id"],
        "params": n["params"], "expected_version": 1})
    assert stale.status_code == 409


def test_normal_conditions_never_lock_account_and_explain_next_step(client, headers):
    for _ in range(4):
        plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000})
        assert plan["nodes"][0]["error"]["category"] == "business"
        assert plan["nodes"][0]["error"]["next_step"]
    assert not client.get("/api/auth/me", headers=headers).json()["locked"]
    payroll(client, headers)
    with client.app.state.db.write() as s:
        s.get(User, "u_demo").risk_level = 3
    purchase = finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_LOCKED", "amount_fen": 50000}))
    assert purchase["status"] == "COMPLETED"
    for _ in range(4):
        blocked = start(client, "wealth.redeem", {"product_id": "P_LOCKED", "amount_fen": 10000})
        assert "2026-11-12" in blocked["nodes"][0]["error"]["message"]
    assert not client.get("/api/auth/me", headers=headers).json()["locked"]


def test_technical_channel_circuit_does_not_lock_user(client, headers):
    payroll(client, headers)
    class BrokenAdapter(SandboxBankAdapter):
        def submit(self, *args):
            raise BankError("BANK_UNAVAILABLE", "银行通道暂不可用")
    client.app.state.plans.adapter = BrokenAdapter(client.app.state.bank)
    for _ in range(3):
        plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000})
        plan = finish(client, headers, plan)
        assert plan["nodes"][0]["error"]["category"] == "technical"
    assert not client.get("/api/auth/me", headers=headers).json()["locked"]
    with client.app.state.db.read() as s:
        assert s.get(Document, "circuit_u_demo").data["open"]
    paused = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000})
    assert paused["nodes"][0]["error"]["code"] == "CHANNEL_CIRCUIT_OPEN"


def test_submit_reply_lost_reconcile_receipt_without_second_debit(client, headers):
    payroll(client, headers)
    client.app.state.plans.adapter = LostResponseSandboxAdapter(client.app.state.bank)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000})
    result = approve(client, headers, plan).json()
    assert result["status"] == "UNKNOWN" and result["nodes"][0]["result"] is None
    assert "不要重复付款" in result["assistant_message"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 520000
    for _ in range(2):
        assert client.post(f"/api/plans/{plan['id']}/resume", headers=headers).json()["status"] == "UNKNOWN"
    assert client.post(f"/api/plans/{plan['id']}/cancel", headers=headers).status_code == 409
    assert client.post(f"/api/plans/{plan['id']}/reconcile", headers=login(client, "wang")).status_code == 404
    recovered = client.post(f"/api/plans/{plan['id']}/reconcile", headers=headers).json()
    assert recovered["status"] == "COMPLETED" and recovered["nodes"][0]["result"]["data"]["amount_fen"] == 80000
    assert client.post(f"/api/plans/{plan['id']}/reconcile", headers=headers).json()["status"] == "COMPLETED"
    with client.app.state.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Receipt).where(Receipt.operation == "funds.transfer")) == 1
        assert s.scalar(select(func.sum(LedgerEntry.delta_fen))) == 0
        assert s.get(Account, "a_demo").balance_fen == 520000


def test_unknown_missing_receipt_stays_unknown_no_resubmit(client, headers):
    payroll(client, headers)
    class PendingAdapter(SandboxBankAdapter):
        calls = 0
        def submit(self, *args):
            self.calls += 1
            raise SubmissionUncertain()
        def lookup(self, *args):
            return LookupResult("PENDING")
    adapter = PendingAdapter(client.app.state.bank)
    client.app.state.plans.adapter = adapter
    p = approve(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 1000})).json()
    for _ in range(3):
        assert client.post(f"/api/plans/{p['id']}/reconcile", headers=headers).json()["status"] == "UNKNOWN"
    assert adapter.calls == 1
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000


def _resume_process(folder, plan_id, output):
    app = create_app(Settings(data_dir=Path(folder), model_mode="demo"))
    try:
        result = app.state.graph.resume("u_demo", plan_id)
        output.put(result["status"])
    except Exception as error:
        output.put(type(error).__name__)
    finally:
        app.state.graph.close()
        app.state.db.engine.dispose()


def _hold_process(folder, output):
    with ProcessLocks(Path(folder)).hold("crash-test"):
        output.put("locked")
        time.sleep(20)


def test_cross_process_graph_resume_is_idempotent(client, headers):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000})
    n = plan["nodes"][0]
    client.app.state.plans.authorize("u_demo", plan["id"], ApprovalRequest(node_id=n["id"], fingerprint=n["fingerprint"]))
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    workers = [ctx.Process(target=_resume_process, args=(str(client.app.state.settings.data_dir), plan["id"], output)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    assert [output.get(timeout=3) for _ in workers] == ["COMPLETED", "COMPLETED"]
    with client.app.state.db.read() as s:
        assert s.get(Account, "a_demo").balance_fen == 520000
        assert s.scalar(select(func.count()).select_from(Receipt).where(Receipt.operation == "funds.transfer")) == 1


def test_os_lock_is_released_when_process_dies(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    child = ctx.Process(target=_hold_process, args=(str(tmp_path), output))
    child.start()
    try:
        assert output.get(timeout=10) == "locked"
        locks = ProcessLocks(tmp_path)
        assert locks.busy("crash-test")
    finally:
        child.terminate()
        child.join(10)
    with locks.hold("crash-test", timeout=1):
        assert locks.busy("crash-test")


def test_other_worker_reads_running_research_without_false_interruption(client, headers):
    service = client.app.state.research
    key = "research_other_worker"
    from bank_agent.storage import save_document
    with client.app.state.db.write() as s:
        save_document(s, key, "research", "u_demo", {"id": key, "version": 1, "status": "RESEARCH_RUNNING",
            "created_at": "2026-01-01T00:00:00+00:00", "conversation_id": "worker-test"})
    with service.banking.process_locks.hold("researchjob:" + key):
        assert service.get("u_demo", key)["status"] == "RESEARCH_RUNNING"
    assert service.get("u_demo", key)["status"] == "RESEARCH_INTERRUPTED"


def test_web_factory_does_not_start_periodic_scheduler(client):
    assert client.get("/health").json()["scheduler_mode"] == "external"
    assert client.app.state.settings.scheduler_mode == "external"


def test_preview_resets_daily_risk_on_scheduled_day(client, headers):
    payroll(client, headers)
    finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}))
    future = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}, scheduled_for="2026-10-14T09:00:00+08:00")
    assert future["funding_preview"]["steps"][0]["tier"] == "YELLOW"
    assert future["funding_preview"]["immediate_after"]["balance_fen"] == 520000
    assert client.get("/api/overview", headers=headers).json()["business_now"].startswith("2026-10-13")


def test_preview_known_recurring_debits_during_wait_are_rollback_only(client, headers):
    payroll(client, headers)
    p = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 80000}, scheduled_for="2026-10-21T09:00:00+08:00")
    preview = p["funding_preview"]
    assert preview["simulated_recurring_debits_fen"] == 4800
    assert preview["after"]["balance_fen"] == 515200
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
    with client.app.state.db.read() as s:
        assert s.get(Document, "S_VIDEO").data["next_debit"] == "2026-10-20"
        assert not list(s.scalars(select(Document).where(Document.kind == "debit_event")))


def test_preview_pending_redemption_only_funds_future_payment_after_settlement(client, headers):
    payroll(client, headers, 100000)
    finish(client, headers, start(client, "wealth.purchase", {"product_id": "P_STEADY", "amount_fen": 100000}))
    plan = client.app.state.graph.start("u_demo", "先赎回，明天到账后转800元", intent=Intent(summary="赎回后预约转账", tasks=[
        {"id": "redeem", "domain": "wealth", "action": "wealth.redeem", "params": {"product_id": "P_STEADY", "amount_fen": 100000}},
        {"id": "transfer", "domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 80000},
         "depends_on": ["redeem"], "scheduled_for": "2026-10-14T09:00:00+08:00"},
    ]))
    preview = plan["funding_preview"]
    assert preview["before"]["available_fen"] == 0 and preview["immediate_after"]["available_fen"] == 0
    assert preview["after"]["available_fen"] == 20000 and preview["ready"]
    assert preview["deferred_inflows"][0]["counted_as_available"] is False
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 0


def test_legacy_business_lock_migration_requires_audit_proof(client):
    from bank_agent.migrations import migrate_failure_policy
    from bank_agent.storage import audit, save_document
    with client.app.state.db.write() as s:
        s.delete(s.get(Document, "migration_failure_policy_v2"))
        for user_id in ["u_demo", "u_wang"]:
            s.get(User, user_id).locked = True
            save_document(s, f"circuit_{user_id}", "circuit", user_id, {"failures": 3})
            for _ in range(3):
                audit(s, user_id, "OPERATION_BLOCKED", {"error": {"code": "INSUFFICIENT_AVAILABLE"}})
            audit(s, user_id, "CIRCUIT_OPEN", {"failures": 3})
        s.get(User, "u_wang").failed_verifications = 3
    migrate_failure_policy(client.app.state.db)
    with client.app.state.db.read() as s:
        assert not s.get(User, "u_demo").locked
        assert s.get(User, "u_wang").locked
        assert s.get(Document, "circuit_u_demo").data["failures"] == 0
