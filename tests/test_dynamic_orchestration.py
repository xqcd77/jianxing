import asyncio
from threading import Event

import pytest
from sqlalchemy import select

from bank_agent.errors import BankError
from bank_agent.orchestration import NodeFSM
from bank_agent.schemas import ApprovalRequest, Intent, NodeSpec, ReplanRequest
from bank_agent.storage import Document, LedgerEntry, Node, Plan
from conftest import approve, finish, payroll, start


def specs(plan):
    return [
        NodeSpec(
            id=n["local_id"],
            agent=n["agent"],
            operation=n["operation"],
            params=n["params"],
            depends_on=n["depends_on"],
            after=n["after"],
            scheduled_for=n["scheduled_for"],
            reason=n["reason"],
        )
        for n in plan["nodes"]
    ]


def two_transfers(client):
    return client.app.state.graph.start(
        "u_demo",
        "转账计划",
        intent=Intent(
            summary="转账计划",
            tasks=[
                {
                    "domain": "funds",
                    "action": "funds.transfer",
                    "params": {"recipient_hint": "妈妈", "amount_fen": amount},
                }
                for amount in [50000, 20000]
            ],
        ),
    )


def test_replan_appends_tasks_preserves_receipt_and_requires_new_approval(client, headers):
    payroll(client, headers)
    plan = approve(client, headers, two_transfers(client)).json()
    done = next(n for n in plan["nodes"] if n["status"] == "SUCCEEDED")
    old_pending = next(n for n in plan["nodes"] if n["status"] == "WAIT_CONFIRM")
    proposal = specs(plan) + [
        NodeSpec(
            id="extra",
            agent="funds",
            operation="funds.transfer",
            params={"recipient_hint": "妈妈", "amount_fen": 10000},
        )
    ]
    result = client.post(
        f"/api/plans/{plan['id']}/replan",
        headers=headers,
        json={
            "expected_version": plan["version"],
            "reason": "再给妈妈转100元",
            "nodes": [s.model_dump() for s in proposal],
        },
    )
    assert result.status_code == 200, result.text
    revised = result.json()
    assert revised["version"] == plan["version"] + 1
    assert next(n for n in revised["nodes"] if n["id"] == done["id"])["result"] == done["result"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 550000
    assert approve(client, headers, plan, old_pending).status_code == 409
    assert finish(client, headers, revised)["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 520000
    client.app.state.graph.resume("u_demo", plan["id"])
    with client.app.state.db.read() as session:
        assert (
            len(
                list(session.scalars(select(LedgerEntry).where(LedgerEntry.receipt_id == done["result"]["receipt_id"])))
            )
            == 2
        )


def test_replan_cannot_rewrite_completed_transfer(client, headers):
    payroll(client, headers)
    plan = approve(client, headers, two_transfers(client)).json()
    proposed = specs(plan)
    proposed[0].params = {**proposed[0].params, "amount_fen": 1}
    response = client.post(
        f"/api/plans/{plan['id']}/replan",
        headers=headers,
        json={
            "expected_version": plan["version"],
            "reason": "修改已完成金额",
            "nodes": [n.model_dump() for n in proposed],
        },
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "TERMINAL_NODE_IMMUTABLE"
    assert client.get(f"/api/plans/{plan['id']}", headers=headers).json()["version"] == plan["version"]


def test_replan_rejects_cycle_and_stale_version_atomically(client, headers):
    payroll(client, headers)
    plan = two_transfers(client)
    proposed = specs(plan)
    proposed[0].depends_on = [proposed[1].id]
    proposed[1].depends_on = [proposed[0].id]
    service = client.app.state.plans
    with pytest.raises(BankError, match="循环"):
        service.replan(
            "u_demo", plan["id"], ReplanRequest(expected_version=plan["version"], reason="循环", nodes=proposed)
        )
    assert service.snapshot("u_demo", plan["id"])["version"] == plan["version"]
    with pytest.raises(BankError) as error:
        service.replan("u_demo", plan["id"], ReplanRequest(expected_version=99, reason="过期版本", nodes=specs(plan)))
    assert error.value.code == "PLAN_VERSION_CONFLICT"


def test_automatic_replan_cannot_change_money(client, headers):
    payroll(client, headers)
    plan = two_transfers(client)
    proposed = specs(plan)
    proposed[0].params = {**proposed[0].params, "amount_fen": 100}
    with pytest.raises(BankError) as error:
        client.app.state.plans.replan(
            "u_demo",
            plan["id"],
            ReplanRequest(
                expected_version=plan["version"],
                reason="擅自降低金额",
                nodes=proposed,
            ),
            automatic=True,
        )
    assert error.value.code == "UNSAFE_AUTOMATIC_REPLAN"


def test_quote_recovery_changes_version_and_mfa_without_replaying_transfer(client, headers):
    payroll(client, headers)
    birthday = finish(
        client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20", "budget_fen": 100000})
    )
    client.post("/api/demo/clock", headers=headers, json={"days": 5})
    transfer = finish(client, headers, start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000}))
    birthday = client.get(f"/api/plans/{birthday['id']}", headers=headers).json()
    completed = {n["id"]: n["result"] for n in birthday["nodes"] if n["status"] == "SUCCEEDED"}
    balance = client.get("/api/overview", headers=headers).json()["account"]["balance_fen"]
    with client.app.state.db.write() as session:
        item = session.get(Document, "FLOWERS")
        item.data = {**item.data, "price_fen": 35000}
    assert approve(client, headers, birthday).status_code == 409
    revised = client.get(f"/api/plans/{birthday['id']}", headers=headers).json()
    purchase = next(n for n in revised["nodes"] if n["operation"] == "life.purchase")
    assert revised["version"] == birthday["version"] + 1
    assert purchase["status"] == "WAIT_MFA" and purchase["params"]["quote"]["total_fen"] == 65000
    assert revised["review"]["automatic_repairs"] == 1
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == balance
    assert {n["id"]: n["result"] for n in revised["nodes"] if n["status"] == "SUCCEEDED"} == completed
    assert finish(client, headers, revised)["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == balance - 65000
    assert (
        client.app.state.plans.snapshot("u_demo", transfer["id"])["nodes"][0]["result"]
        == transfer["nodes"][0]["result"]
    )


def test_repair_budget_exhaustion_pauses_for_human(client, headers):
    payroll(client, headers)
    plan = finish(client, headers, start(client, "life.goal", {"birthday_date": "2026-10-20"}))
    client.post("/api/demo/clock", headers=headers, json={"days": 5})
    with client.app.state.db.write() as session:
        stored = session.get(Plan, plan["id"])
        stored.review = {**stored.review, "automatic_repairs": 3}
        item = session.get(Document, "FLOWERS")
        item.data = {**item.data, "price_fen": 21000}
    updated = client.app.state.graph.resume("u_demo", plan["id"])
    assert updated["status"] == "WAIT_HUMAN" and updated["review"]["human_handoff"]["status"] == "PENDING"


def test_explicit_negative_constraint_blocks_proposed_redemption(client, headers):
    plan = client.app.state.graph.start(
        "u_demo",
        "不要赎回理财，只查看持仓",
        intent=Intent(
            summary="查询",
            tasks=[
                {"domain": "wealth", "action": "wealth.redeem", "params": {"product_id": "P_CASH", "amount_fen": 100}},
                {"domain": "wealth", "action": "wealth.holdings", "params": {}},
            ],
        ),
    )
    assert plan["status"] == "COMPLETED"
    assert [n["operation"] for n in plan["nodes"]] == ["wealth.holdings"]


def test_verifier_still_blocks_a_forbidden_node_if_planner_gate_is_bypassed(client):
    graph = client.app.state.graph
    key = graph.plans.create("u_demo", "negative-verifier", "negative-verifier", "独立核验", [
        NodeSpec(id="blocked",agent="wealth",operation="wealth.redeem",params={"product_id":"P_CASH","amount_fen":100})
    ], {"protected":{"forbidden_operations":["wealth.redeem"]}})
    graph.orchestrator.verify("u_demo", key)
    blocked = graph.plans.snapshot("u_demo", key)["nodes"][0]
    assert blocked["status"] == "WAIT_INPUT"
    assert blocked["error"]["code"] == "USER_CONSTRAINT" and not blocked["result"]


@pytest.mark.parametrize("status", ["SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"])
def test_terminal_node_state_cannot_be_reopened(status):
    with pytest.raises(BankError) as error:
        NodeFSM.move(Node(status=status), "READY")
    assert error.value.code == "TERMINAL_NODE_IMMUTABLE"


def test_async_tasks_concurrent_and_cancellation_prevents_business_commit(client, monkeypatch):
    graph, reached = client.app.state.graph, []

    async def planner(user_id, message, conversation_id=None):
        reached.append(message)
        while len(reached) < 2:
            await asyncio.sleep(0.005)
        return Intent(summary="余额", tasks=[{"domain": "funds", "action": "balance.query", "params": {}}])

    monkeypatch.setattr(graph.planner, "aplan", planner)

    async def concurrent():
        return await asyncio.wait_for(asyncio.gather(graph.astart("u_demo", "a"), graph.astart("u_demo", "b")), 5)

    assert all(p["status"] == "COMPLETED" for p in asyncio.run(concurrent()))

    async def waiting(*args):
        await asyncio.sleep(30)

    monkeypatch.setattr(graph.planner, "aplan", waiting)

    async def cancelled():
        task = asyncio.create_task(graph.astart("u_demo", "待取消规划"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    asyncio.run(cancelled())
    with client.app.state.db.read() as session:
        assert len(list(session.scalars(select(Plan)))) == 2


def test_cancellation_after_bank_commit_can_resume_without_double_posting(client, headers, monkeypatch):
    payroll(client, headers)
    plan = start(client, "funds.transfer", {"recipient_hint": "妈妈", "amount_fen": 50000})
    node = plan["nodes"][0]
    service, graph = client.app.state.plans, client.app.state.graph
    service.authorize("u_demo", plan["id"], ApprovalRequest(node_id=node["id"], fingerprint=node["fingerprint"]))
    committed, release = Event(), Event()
    original = service.step

    def step(*args):
        result = original(*args)
        committed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(service, "step", step)

    async def cancel_after_commit():
        task = asyncio.create_task(graph.aresume("u_demo", plan["id"]))
        assert await asyncio.to_thread(committed.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_after_commit())
    monkeypatch.setattr(service, "step", original)
    recovered = graph.resume("u_demo", plan["id"])
    assert recovered["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 550000
    with client.app.state.db.read() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(LedgerEntry).where(
                            LedgerEntry.receipt_id == recovered["nodes"][0]["result"]["receipt_id"],
                        )
                    )
                )
            )
            == 2
        )


def test_natural_language_append_reuses_plan_and_waits_for_new_authorization(client, headers):
    payroll(client, headers)
    first = client.post("/api/chat", headers=headers, json={"message": "给妈妈转500元"}).json()
    appended = client.post(
        "/api/chat",
        headers=headers,
        json={
            "message": "再给妈妈转100元",
            "conversation_id": first["conversation_id"],
        },
    )
    assert appended.status_code == 200, appended.text
    revised = appended.json()
    assert revised["id"] == first["id"] and revised["version"] == first["version"] + 1
    assert len(revised["nodes"]) == 2
    assert approve(client, headers, first).status_code == 409
    assert finish(client, headers, revised)["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 540000


def test_replan_rejects_credentials_inside_business_notes_before_execution(client, headers):
    payroll(client, headers)
    plan = two_transfers(client)
    proposed = specs(plan)
    proposed[0].params = {**proposed[0].params, "subject": "sk-do-not-store"}
    response = client.post(
        f"/api/plans/{plan['id']}/replan",
        headers=headers,
        json={
            "expected_version": plan["version"],
            "reason": "修改备注",
            "nodes": [s.model_dump() for s in proposed],
        },
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "SENSITIVE_BUSINESS_INPUT"
    assert client.app.state.plans.snapshot("u_demo", plan["id"])["version"] == plan["version"]


def test_dynamic_budget_is_reserved_before_pending_transfer(client, headers):
    payroll(client, headers)
    first = client.post("/api/chat", headers=headers, json={"message": "给妈妈转500元"}).json()
    proposed = specs(first) + [
        NodeSpec(
            id="budget", agent="funds", operation="funds.reserve", params={"amount_fen": 580000, "purpose": "生活费"}
        )
    ]
    response = client.post(
        f"/api/plans/{first['id']}/replan",
        headers=headers,
        json={
            "expected_version": first["version"],
            "reason": "先保留生活费",
            "nodes": [s.model_dump() for s in proposed],
        },
    )
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["nodes"][0]["operation"] == "funds.reserve"
    assert revised["funding_preview"]["shortfall_fen"] == 30000
    assert not revised["confirmation_batch"]["available"]
    transfer = next(n for n in revised["nodes"] if n["operation"] == "funds.transfer")
    assert transfer["status"] == "FAILED" and transfer["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 600000
