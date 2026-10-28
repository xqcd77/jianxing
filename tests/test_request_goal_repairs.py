"""User-reported amount binding, mixed goals and research follow-up regressions."""

import asyncio

import pytest
from sqlalchemy import select

from bank_agent.dialogue import transfer_grounding
from bank_agent.schemas import Intent
from bank_agent.storage import Document, Receipt, Transaction
from conftest import finish, login, payroll
from test_public_information import ask, counts, source
from test_research import research


ETF_DIRECTORY = """指数型
<a href='/Services/cn/html/product/index.shtml?fundcode=159919'>嘉实沪深300ETF</a>
<a href='/Services/cn/html/product/index.shtml?fundcode=160706'>嘉实沪深300ETF联接A</a>
<a href='/Services/cn/html/product/index.shtml?fundcode=000083'>嘉实测试债券A</a>"""


@pytest.mark.parametrize("message", [
    "我的4500元 给妈转1200 剩下的给我推荐一款ETF产品",
    "我有8千元，给母亲转1500元，其余推荐ETF",
])
def test_compound_goal_is_never_grounded_as_one_transfer(client, message):
    facts = client.app.state.bank.facts("u_demo")
    assert transfer_grounding(message, facts, {}) is None


def test_explicit_transfer_amount_is_bound_to_action_not_total(client):
    planner = client.app.state.graph.planner
    facts = planner.bank.facts("u_demo")
    grounded = transfer_grounding("我的4500元 给妈转1200", facts, {})
    assert grounded.tasks[0].params["amount_fen"] == 120000
    wrong = Intent(summary="错误模型金额", tasks=[{
        "domain": "funds", "action": "funds.transfer",
        "params": {"recipient_hint": "妈妈", "amount_fen": 450000},
    }])
    reconciled = planner._reconcile("u_demo", "test", None, wrong,
        "我的4500元 给妈转1200 剩下的给我推荐一款ETF产品")
    assert reconciled.tasks[0].params["amount_fen"] == 120000


def test_transfer_and_etf_goal_survive_confirmation_and_refresh(client, headers):
    source(client, funds=ETF_DIRECTORY)
    payroll(client, headers, 450000)
    plan = ask(client, headers, "我的4500元 给妈转1200 剩下的给我推荐一款ETF产品")
    assert plan["status"] in {"WAIT_CONFIRM", "WAIT_MFA"}, plan
    assert [(n["operation"], n["params"]["amount_fen"]) for n in plan["nodes"]] == [("funds.transfer", 120000)]
    assert len(plan["request_goals"]) == 2
    advisory = plan["request_goals"][1]["result"]
    assert "ETF" in advisory["message"] and "3300" in advisory["message"].replace(",", "")
    assert advisory["investment_budget"]["budget_fen"] == 330000
    assert advisory["candidates"][0]["code"] == "159919"
    assert all("联接" not in c["name"] for c in advisory["candidates"])
    assert "ETF" in plan["assistant_message"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 450000
    completed = finish(client, headers, plan)
    assert completed["status"] == "COMPLETED"
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 330000
    refreshed = client.get("/api/plans/" + plan["id"], headers=headers).json()
    assert refreshed["request_goals"][0]["status"] == "SUCCEEDED"
    assert refreshed["request_goals"][1]["result"] == advisory
    with client.app.state.db.read() as session:
        receipts = list(session.scalars(select(Receipt).where(Receipt.owner_id == "u_demo", Receipt.operation == "funds.transfer")))
        assert len(receipts) == 1
        transaction = session.scalar(select(Transaction).where(Transaction.receipt_id == receipts[0].id))
        assert transaction.amount_fen == 120000 and transaction.recipient == "a_mom"


def test_daily_etf_request_uses_public_sources_and_separate_budget_roles(client, headers):
    source(client, funds=ETF_DIRECTORY)
    payroll(client, headers, 600000)
    before = counts(client)
    result = ask(client, headers, "我的6000元 给我推荐一款适合每日定投200元的ETF")
    assert result["status"] == "ANSWERED", result
    assert result["investment_budget"] == {
        "budget_fen": 600000, "periodic_amount_fen": 20000, "full_contributions": 30,
        "source": "user_statement", "financial_operations_executed": False,
    }
    assert "200" in result["message"] and "30" in result["message"]
    assert result["candidates"][0]["code"] == "159919"
    assert "交易" in result["message"] and "风险" in result["message"]
    assert counts(client) == before and client.get("/api/research", headers=headers).json() == []
    followup = ask(client, headers, "好了吗", result["conversation_id"])
    assert followup["candidates"] == result["candidates"] and followup["investment_budget"] == result["investment_budget"]
    assert counts(client) == before


def test_unavailable_etf_source_preserves_second_goal_and_never_changes_transfer(client, headers):
    source(client, status=503)
    payroll(client, headers, 450000)
    result = ask(client, headers, "我的4500元 给妈转1200 剩下的给我推荐一款ETF产品")
    assert result["nodes"][0]["params"]["amount_fen"] == 120000
    assert result["request_goals"][1]["status"] == "UNAVAILABLE"
    advisory = result["request_goals"][1]["result"]
    assert advisory["investment_budget"]["budget_fen"] == 330000
    assert "未能取得可核验" in advisory["message"] and not advisory.get("candidates")


def test_editing_transfer_marks_previous_etf_budget_as_needing_recheck(client, headers):
    source(client, funds=ETF_DIRECTORY)
    payroll(client, headers, 450000)
    result = ask(client, headers, "我的4500元 给妈转1200 剩下的给我推荐一款ETF产品")
    node = result["nodes"][0]
    response = client.post("/api/plans/" + result["id"] + "/edit", headers=headers, json={
        "node_id": node["id"], "params": {**node["params"], "amount_fen": 100000}, "expected_version": result["version"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["request_goals"][1]["status"] == "NEEDS_RECHECK"


@pytest.mark.parametrize("message,budget,contribution", [
    ("推荐一款每日定投200元的ETF", None, 20000),
    ("我的100000元，推荐ETF，每天定投100元", 10000000, 10000),
    ("我的6000元，推荐ETF代码159919，每天定投200元", 600000, 20000),
    ("我的6,000元，推荐ETF，每天定投200元", 600000, 20000),
])
def test_etf_amounts_and_codes_are_not_confused(client, headers, message, budget, contribution):
    source(client, funds=ETF_DIRECTORY)
    result = ask(client, headers, message)
    assert result["investment_budget"]["budget_fen"] == budget
    assert result["investment_budget"]["periodic_amount_fen"] == contribution
    assert result["candidates"][0]["code"] == "159919"


def test_running_research_followup_reads_same_job_without_model(client, headers, monkeypatch):
    service = client.app.state.research

    async def hold(*_args):
        await asyncio.sleep(3600)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("A progress question must read persisted state, without a model call")

    monkeypatch.setattr(service, "_run_locked", hold)
    monkeypatch.setattr(service.model, "astructured", forbidden)
    started = research(client, headers, wait=False)
    try:
        with client.app.state.db.read() as session:
            dialogue = session.get(Document, "dialogue_u_demo_" + started["conversation_id"])
            assert dialogue.data["active_research_id"] == started["id"]
        for text in ["好了吗", "研究完了吗？", "进度怎么样"]:
            result = ask(client, headers, text, started["conversation_id"])
            assert result["id"] == started["id"] and result["status"] == "RESEARCH_RUNNING"
            assert "阶段" in result["message"]
        assert len(client.get("/api/research", headers=headers).json()) == 1
        other = ask(client, login(client, "wang"), "好了吗", started["conversation_id"])
        assert other.get("id") != started["id"] and "report" not in other
    finally:
        client.post("/api/research/" + started["id"] + "/cancel", headers=headers)


def test_completed_research_followup_returns_original_report(client, headers):
    completed = research(client, headers)
    result = ask(client, headers, "分析结果呢", completed["conversation_id"])
    assert result["id"] == completed["id"]
    assert result["report"] == completed["report"]
    assert len(client.get("/api/research", headers=headers).json()) == 1


def test_cancelled_research_followup_reports_cancelled_without_new_job(client, headers, monkeypatch):
    async def hold(*_args):
        await asyncio.sleep(3600)
    monkeypatch.setattr(client.app.state.research, "_run_locked", hold)
    started = research(client, headers, wait=False)
    cancelled = client.post("/api/research/" + started["id"] + "/cancel", headers=headers)
    assert cancelled.status_code == 200
    result = ask(client, headers, "好了吗", started["conversation_id"])
    assert result["status"] == "RESEARCH_CANCELLED" and result["id"] == started["id"]
    assert len(client.get("/api/research", headers=headers).json()) == 1


def test_late_research_completion_does_not_erase_newer_public_information_context(client, headers):
    from bank_agent.dialogue import record_turn
    source(client, funds=ETF_DIRECTORY)
    completed = research(client, headers)
    answer = ask(client, headers, "我的6000元 推荐每日定投200元的ETF", completed["conversation_id"])
    record_turn(client.app.state.db, "u_demo", completed["conversation_id"], "旧研究", {
        "status": "RESEARCH_READY", "message": "旧研究完成", "research_id": completed["id"],
    })
    followup = ask(client, headers, "好了吗", completed["conversation_id"])
    assert followup["investment_budget"] == answer["investment_budget"]
    assert "report" not in followup


def test_etf_foreign_currency_is_not_treated_as_cny(client, headers):
    requests = source(client, funds=ETF_DIRECTORY)
    result = ask(client, headers, "我的6000美元 推荐每日定投200美元的ETF")
    assert result["investment_budget"]["budget_fen"] is None
    assert "不能把外币金额当成人民币" in result["message"] and not requests


def test_rule_path_also_binds_amount_with_multiple_goals(client, headers):
    payroll(client, headers, 450000)
    plan = ask(client, headers, "我的4500元 给妈转1200 并且查询余额")
    transfers = [n for n in plan["nodes"] if n["operation"] == "funds.transfer"]
    assert len(transfers) == 1 and transfers[0]["params"]["amount_fen"] == 120000


def test_plan_preview_serializes_with_concurrent_runtime_writes_and_rolls_back(client, headers, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from bank_agent.storage import audit

    payroll(client, headers, 450000)
    plan = ask(client, headers, "给妈妈转1200元")
    before = counts(client)
    db, bank = client.app.state.db, client.app.state.bank
    entered, writing, written = Event(), Event(), Event()
    original = bank.dispatch

    def dispatch(*args, **kwargs):
        if args[2] == "cashflow.project" and not entered.is_set():
            entered.set()
            assert writing.wait(3)
            written.wait(.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(bank, "dispatch", dispatch)

    def telemetry_write():
        assert entered.wait(3)
        writing.set()
        with db.write() as session:
            audit(session, "u_demo", "PREVIEW_CONCURRENT_REQUEST", {})
        written.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        preview = executor.submit(client.app.state.plans.snapshot, "u_demo", plan["id"])
        writer = executor.submit(telemetry_write)
        assert preview.result(timeout=6)["funding_preview"]["after"]["balance_fen"] == 330000
        writer.result(timeout=6)
    assert written.is_set() and counts(client) == before
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 450000
