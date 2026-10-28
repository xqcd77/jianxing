"""Current-arm regressions use changed amounts and preserve baseline isolation."""

from decimal import Decimal

import pytest

from bank_agent.config import Settings
from bank_agent.deep_banking import DeepBankingGraph, DeepPlannerAgent, educational_answer
from bank_agent.deep_research import DeepResearchService, ResearchAnswer, calculate, safe_derive
from bank_agent.errors import BankError
from bank_agent.graph import BankingGraph
from bank_agent.agents import PlannerAgent
from bank_agent.research_schemas import ResearchBrief
from bank_agent.storage import Document
from evaluation.variants import create_variant_app, OnePassResearchService, SingleOnePassResearchService


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("9000-2300-1700-450", Decimal("4550")),
        ("round(1200*1.031*0.01,2)", Decimal("12.37")),
        ('hours_between("2026-10-22T09:00:00+08:00","2026-10-25T18:00:00+08:00")', Decimal(81)),
        ('add_days("2026-10-18T18:00:00+08:00",7)', "2026-10-25T18:00:00+08:00"),
    ],
)
def test_restricted_calculation(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize(
    "expression", ['__import__("os")', "(1).__class__", "2**1000000", "[1]*999999", 'open("file")', "1/0"]
)
def test_calculator_rejects_code_and_invalid_math(expression):
    with pytest.raises((ValueError, ZeroDivisionError, ArithmeticError)):
        calculate(expression)


def test_unit_elision_and_contextual_date_grounding():
    brief = ResearchBrief(reserve_fen=230000, emergency_fen=170000, amount_fen=450000, needed_on="2026-10-21")
    DeepResearchService._ground(brief, "生活费留2300，应急1700，拟投4500，21日周三9点用钱", "2026-10-13T09:00:00+08:00")
    with pytest.raises(BankError):
        DeepResearchService._ground(ResearchBrief(amount_fen=999000), "我有2300元闲钱", "2026-10-13T09:00:00+08:00")


def test_unknown_terms_are_not_zero_fee_or_same_day(client):
    service = client.app.state.research
    with service.db.write() as session:
        p = session.get(Document, "P_CASH")
        p.data = {**p.data, "fee_bps": None, "redeem_business_days": None}
    brief = ResearchBrief(topic="wealth", analysis_only=True, forbid_purchase=True)
    evidence = {}
    for tool in ["constraints", "account", "wealth.compare", "cashflow.project", "product_terms"]:
        ev = service.tools.read("u_demo", "wealth", tool, brief)
        evidence[ev["id"]] = ev
    result = safe_derive("wealth", evidence, brief)
    assert any("未知" in c["text"] and c["values"].get("product_id") == "P_CASH" for c in result["claims"])
    assert not any(p["product_id"] == "P_CASH" for p in result["products"])
    assert not result["options"]
    assert brief.forbid_purchase  # Protected user prohibition remains intact.


def test_synthesized_claims_require_real_sources_and_checked_values():
    evidence = {"ev1": {"id": "ev1", "data": {"supporting_records": [{"source_id": "contract1"}]}}}
    answer = ResearchAnswer(
        summary="当前可安排的资金是{{c1}}，没有执行操作。",
        calculations=[{"id": "c1", "expression": "9000-2300-1700-450", "evidence_ids": ["contract1"]}],
        points=[{"title": "资金计算", "text": "依次扣减预留和支出后剩{{c1}}。", "evidence_ids": ["ev1"]}],
        limitations="未登记支出不在本次范围内。",
    )
    answer.summary = "当前可安排的资金是{{c1}}元，没有执行操作。"
    findings, calcs = DeepResearchService._checked_answer(answer, evidence, "余额9000，留2300、1700，支出450")
    assert "4,550.00元" in findings[0]["text"] and calcs[0]["result"] == "4550"
    assert "元元" not in findings[0]["text"]
    assert findings[0]["evidence_ids"] == ["ev1"]
    answer.points[0].evidence_ids = ["invented_source"]
    with pytest.raises(ValueError):
        DeepResearchService._checked_answer(answer, evidence)


def test_calculation_cannot_smuggle_an_authored_answer_as_operand():
    evidence = {"ev1": {"id": "ev1", "data": {"balance_fen": 900000}}}
    answer = ResearchAnswer(
        summary="本次只分析资金，不进行付款。",
        calculations=[{"id": "c1", "expression": "8765+0", "evidence_ids": ["ev1"]}],
        points=[{"title": "核查", "text": "资金{{c1}}。", "evidence_ids": ["ev1"]}],
        limitations="不包括未知支出。",
    )
    with pytest.raises(ValueError):
        DeepResearchService._checked_answer(answer, evidence, "查资金")


def test_real_document_id_is_a_valid_citation_alias():
    evidence = {"ev1": {"id": "ev1", "source_ids": ["product_doc"], "data": {"minimum_fen": 10000}}}
    answer = ResearchAnswer(
        summary="仅核查已读取产品的起购资料，不执行购买。",
        points=[{"title": "起购", "text": "起购100元。", "evidence_ids": ["product_doc"]}],
        limitations="不保证收益。",
    )
    findings, _ = DeepResearchService._checked_answer(answer, evidence)
    assert findings[1]["evidence_ids"] == ["ev1"]


def test_current_repairs_do_not_replace_baseline_classes(tmp_path):
    from fastapi.testclient import TestClient

    for variant, research_cls in [
        ("S_AGENT", SingleOnePassResearchService),
        ("M_AGENT", OnePassResearchService),
        ("C", DeepResearchService),
    ]:
        app = create_variant_app(Settings(data_dir=tmp_path / variant), variant)
        with TestClient(app):
            assert type(app.state.research) is research_cls
            assert type(app.state.graph.planner) is (DeepPlannerAgent if variant == "C" else PlannerAgent)
            if variant == "M_AGENT":
                assert type(app.state.graph) is BankingGraph
            if variant == "C":
                assert type(app.state.graph) is DeepBankingGraph


def test_installment_cost_does_not_require_unnecessary_dates():
    answer = educational_answer("本金24000元分8期，每期另收90元手续费，按月还款但具体日期没给，真实成本是多少？")
    assert "720元" in answer and "3.00%" in answer and "IRR" in answer and "没有办理分期" in answer


def test_balance_explanation_does_not_authorize_pending_payment():
    answer = educational_answer("总余额与可用余额有什么不同，先解释一下，别付款。")
    assert all(word in answer for word in ["预留", "冻结", "尚未到账", "没有付款", "原办理计划仍保留"])
    assert educational_answer("给妈妈转500元，看看可用余额和总余额") is None


def math_state(query, sources=(), available=800000, reserve=0, amount=None):
    return {
        "query": query,
        "brief": ResearchBrief(reserve_fen=reserve, amount_fen=amount).model_dump(),
        "evidence": {
            "account": {
                "id": "acc",
                "tool": "account",
                "data": {"available_fen": available, "balance_fen": available, "risk_level": 2},
            },
            "cashflow": {
                "id": "cf",
                "tool": "cashflow.project",
                "data": {"pending_plans": [], "known_future_debits_fen": 0, "supporting_records": list(sources)},
            },
        },
    }


def test_money_scaling_and_unknowns_in_model_view():
    from bank_agent.research_math import model_units

    converted = model_units({"amount_fen": 123456, "fee_bps": 100, "nav_1e4": 10200, "redeem_business_days": None})
    assert converted == {
        "amount_yuan": "1234.56",
        "fee_pct": "1",
        "fee_ratio": "0.01",
        "nav": "1.02",
        "redeem_business_days": None,
    }


def test_cashflow_scenarios_use_independent_event_clocks():
    from bank_agent.research_math import verified_financial_facts

    sources = [
        {
            "source_id": "events",
            "body": {
                "items": [
                    {"at": "2026-10-15T09:00:00+08:00", "amount_fen": 50000, "kind": "expense"},
                    {"at": "2026-10-18T18:00:00+08:00", "amount_fen": 200000, "kind": "expected_income"},
                    {"at": "2026-10-20T09:00:00+08:00", "amount_fen": 20000, "kind": "subscription"},
                    {"at": "2026-10-22T09:00:00+08:00", "amount_fen": 160000, "kind": "expense"},
                ]
            },
        },
        {
            "source_id": "conditions",
            "body": {
                "income_delay_calendar_days": 7,
                "subscription_increase_pct": 20,
                "other_expenses_increase_pct": 0,
            },
        },
    ]
    facts = verified_financial_facts(math_state("只比较两个情景", sources, available=180000))
    baseline, stress = [f for f in facts if f["kind"] == "timestamp_cashflow"]
    assert baseline["ending_yuan"] == "1500" and baseline["maximum_funding_gap_yuan"] == "0"
    assert stress["maximum_funding_gap_yuan"] == "540" and stress["gap_hours"] == "81.0"
    assert stress["first_gap_at"] == "2026-10-22T09:00:00+08:00"
    assert stress["events"][1]["projected_cash_yuan"] == "1060"


def test_newest_transfer_correction_keeps_living_reserve():
    from bank_agent.research_math import verified_financial_facts

    state = math_state("此前给妈妈800，拟投2000。最新给妈妈改成500元，理财删掉。", reserve=300000)
    budget = verified_financial_facts(state)[0]
    assert budget["new_proposed_transfer_yuan"] == "500"
    assert budget["unallocated_after_all_targets_yuan"] == "4500"


def test_declared_expense_is_budgeted_and_registered_item_not_duplicated():
    from bank_agent.research_math import verified_financial_facts

    state = math_state("核对生活费及未来支出", available=500000, reserve=100000)
    state["brief"]["expenses"] = [
        {"label": "房租", "date": "2026-10-20", "amount_fen": 200000},
        {"label": "订阅", "date": "2026-10-21", "amount_fen": 10000},
    ]
    state["evidence"]["cashflow"]["data"].update(
        known_future_debits_fen=10000,
        obligations=[{"date": "2026-10-21", "amount_fen": 10000, "name": "订阅", "kind": "subscription"}],
    )
    budget = verified_financial_facts(state)[0]
    assert budget["additional_user_declared_expenses_yuan"] == "2000"
    assert budget["maximum_new_investment_yuan"] == "1900"


def test_same_date_and_amount_different_purpose_not_deduplicated():
    from bank_agent.research_math import verified_financial_facts

    state = math_state("另外一笔费用", available=100000)
    state["brief"]["expenses"] = [{"label": "水费", "date": "2026-10-21", "amount_fen": 10000}]
    state["evidence"]["cashflow"]["data"].update(
        known_future_debits_fen=10000,
        obligations=[{"date": "2026-10-21", "amount_fen": 10000, "name": "电费", "kind": "subscription"}],
    )
    assert verified_financial_facts(state)[0]["maximum_new_investment_yuan"] == "800"


def test_assembly_respects_global_disabled_reasoning_and_token_ceiling(tmp_path):
    from bank_agent.deep_research import DeepResearchModel

    model = DeepResearchModel(
        Settings(data_dir=tmp_path, model_api_key="test", model_thinking=False, model_max_tokens=1024)
    )
    request = model._request("analyst", {}, ResearchAnswer)
    assert request["thinking"] == {"type": "disabled"} and "reasoning_effort" not in request
    assert request["max_tokens"] == 1024
    assert model._request("supervisor", {}, ResearchBrief)["max_tokens"] == 1024


def test_position_fees_are_based_on_shares_not_new_purchase_minimum():
    from bank_agent.research_math import verified_financial_facts

    sources = [
        {
            "source_id": "holding",
            "body": {"product_id": "P", "shares": "500", "unlock_at": "2026-10-16T10:00:00+08:00"},
        },
        {"source_id": "nav", "body": {"product_id": "P", "nav": "2.0000", "nav_date": "2026-10-12"}},
        {"source_id": "fee", "body": {"product_id": "P", "redemption_fee_bps": 50, "new_purchase_minimum_fen": 900000}},
    ]
    position = next(
        f
        for f in verified_financial_facts(math_state("至少990元", sources))
        if f["kind"] == "existing_position_redemption"
    )
    assert position["gross_yuan"] == "1000" and position["fee_yuan"] == "5" and position["net_yuan"] == "995"
    assert position["surplus_yuan"] == "5" and position["minimum_nav_for_target"] == "1.9899"
