import asyncio
from dataclasses import replace

import pytest
import httpx

from bank_agent.deep_research import DeepResearchService, ResearchAnswer
from bank_agent.schemas import Intent
from bank_agent.supervisor_agents import BusinessModel, IndependentVerifier, ReviewDecision
from bank_agent.supervisor_math import checked_details, financial_facts, fact_claims
from conftest import payroll
from test_deep_research_repairs import math_state


def test_named_transfer_also_in_declared_expenses_is_not_subtracted_twice():
    state = math_state("生活费留2100，给妈妈700，拟投1800", available=430000, reserve=210000, amount=180000)
    state["brief"]["expenses"] = [{"label": "给妈妈", "date": "2026-10-13", "amount_fen": 70000}]
    budget = financial_facts(state)[0]
    assert budget["maximum_new_investment_yuan"] == "1500"
    assert budget["whole_plan_required_yuan"] == "4600"
    assert budget["whole_plan_gap_yuan"] == "300"
    assert "待你选择" in fact_claims(state)[0]["text"]


def test_matching_amount_but_different_obligation_not_deduplicated():
    state = math_state("给妈妈700元", available=430000)
    state["brief"]["expenses"] = [{"label": "房租", "date": "2026-10-20", "amount_fen": 70000}]
    assert financial_facts(state)[0]["additional_user_declared_expenses_yuan"] == "700"


def test_round_precision_is_syntax_not_an_unsourced_financial_operand():
    evidence = {"ev1": {"id": "ev1", "data": {"shares": "1200", "nav": "1.025", "fee_bps": 100}}}
    answer = ResearchAnswer(
        summary="本次仅估算持仓，不提交赎回申请。",
        calculations=[
            {"id": "c1", "expression": "round(1200*1.025*(1-0.01),4)", "unit": "净值", "evidence_ids": ["ev1"]}
        ],
        points=[{"title": "测算", "text": "{{c1}}", "evidence_ids": ["ev1"]}],
        limitations="未来净值不保证。",
    )
    assert DeepResearchService._checked_answer(answer, evidence)[1][0]["result"] == "1217.7000"
    answer.calculations[0].expression = "round(8765,4)"
    with pytest.raises(ValueError):
        DeepResearchService._checked_answer(answer, evidence)


def test_redemption_deadline_is_before_morning_use_and_respects_holiday():
    sources = [
        {
            "source_id": "terms",
            "body": {
                "product_id": "X",
                "cutoff_exclusive": "14:00:00",
                "same_business_day_settlement_time": "18:00:00",
            },
        }
    ]
    state = math_state("10月27日9点用款", sources)
    state["evidence"]["account"].update(as_of="2026-10-13T09:00:00+08:00")
    state["evidence"]["account"]["data"]["banking_calendar"] = {"closed_dates": ["2026-10-26"]}
    state["research_details"] = {"needed_at": "2026-10-27T09:00:00+08:00"}
    timing = next(f for f in financial_facts(state) if f["kind"] == "redemption_timing")
    assert timing["latest_apply_before"] == "2026-10-23T14:00:00+08:00"
    assert timing["latest_arrival_at"] == "2026-10-23T18:00:00+08:00"


def test_locked_position_and_t_plus_one_use_owned_calendar():
    sources = [
        {
            "source_id": "position",
            "body": {"product_id": "X", "shares": "1200", "unlock_at": "2026-10-16T11:00:00+08:00"},
        },
        {
            "source_id": "terms",
            "body": {
                "product_id": "X",
                "cutoff_exclusive": "14:00:00",
                "settlement_business_days": 1,
                "settlement_time": "16:00:00",
            },
        },
        {"source_id": "calendar", "body": {"business_dates": ["2026-10-13", "2026-10-16", "2026-10-19", "2026-10-20"]}},
    ]
    state = math_state("只核算", sources)
    state["evidence"]["account"]["as_of"] = "2026-10-13T09:00:00+08:00"
    timing = next(f for f in financial_facts(state) if f["kind"] == "redemption_timing")
    assert timing["earliest_arrival_at"] == "2026-10-19T16:00:00+08:00"
    assert timing["deadline_at"] is None


def test_alternative_cross_income_matrix_never_pools_cash():
    source = {
        "source_id": "events",
        "body": {
            "salary_expected_at": "2026-10-18T18:00:00+08:00",
            "salary_fen": 350000,
            "payment_at": "2026-10-20T09:00:00+08:00",
            "payment_fen": 220000,
        },
    }
    state = math_state("甲留2800买2700，乙留800买4700，工资延期6天，只分析不投资", [source], available=550000)
    state["research_details"] = checked_details(
        {
            "income_delay_days": 6,
            "alternatives": [
                {"name": "甲", "retained_cash_fen": 280000, "purchase_fen": 270000},
                {"name": "乙", "retained_cash_fen": 80000, "purchase_fen": 470000},
            ],
        },
        state["query"],
        None,
    )
    rows = [f for f in financial_facts(state) if f["kind"] == "alternative_cashflow"]
    assert len(rows) == 4
    assert [r["cash_after_payment_yuan"] for r in rows] == ["4100", "600", "2100", "-1400"]
    assert rows[-1]["funding_gap_yuan"] == "1400"
    assert rows[-1]["income_at"] == "2026-10-24T18:00:00+08:00"


def test_linked_holding_index_and_distinct_cutoffs_fund_deadline():
    sources = [
        {
            "source_id": "index",
            "body": {
                "slow": {"terms_id": "slow_terms", "value_fen": 420000},
                "fast": {"terms_id": "fast_terms", "value_fen": 180000},
            },
        },
        {
            "source_id": "slow_terms",
            "body": {
                "cutoff_exclusive": "14:00:00",
                "settlement_business_days_after_acceptance": 1,
                "settlement_time": "16:00:00",
                "fee_bps": 0,
            },
        },
        {
            "source_id": "fast_terms",
            "body": {
                "cutoff_exclusive": "16:00:00",
                "same_business_day_settlement_time": "17:00:00",
                "remaining_daily_limit_fen": 160000,
                "fee_bps": 0,
            },
        },
        {"source_id": "calendar", "body": {"business_dates": ["2026-10-16", "2026-10-19", "2026-10-20"]}},
    ]
    state = math_state("20日9点需要4000元，只分析不赎回", sources, available=90000)
    state["evidence"]["account"]["as_of"] = "2026-10-16T15:30:00+08:00"
    state["research_details"] = {"needed_at": "2026-10-20T09:00:00+08:00"}
    facts = financial_facts(state)
    slow, fast = [f for f in facts if f["kind"] == "redemption_timing"]
    assert slow["earliest_arrival_at"] == "2026-10-20T16:00:00+08:00"
    assert fast["earliest_arrival_at"] == "2026-10-16T17:00:00+08:00"
    liquidity = next(f for f in facts if f["kind"] == "deadline_liquidity")
    assert liquidity["maximum_available_yuan"] == "2500" and liquidity["gap_yuan"] == "1500"


def test_model_extracted_scenario_cannot_invent_amount_or_use_time():
    with pytest.raises(ValueError):
        checked_details(
            {"alternatives": [{"name": "甲", "retained_cash_fen": 99900, "purchase_fen": 10000}]}, "甲留200买100", None
        )
    with pytest.raises(ValueError):
        checked_details({"needed_at": "2026-10-20T08:00:00+08:00"}, "10月20日9点", "2026-10-20")
    with pytest.raises(ValueError):
        checked_details({"needed_at": "2026-10-20T09:30:00+08:00"}, "10月20日9点", "2026-10-20")
    assert checked_details({"needed_at": "2026-10-20T09:30:00+08:00"}, "10月20日9点半", "2026-10-20")["needed_at"]


def test_invalid_verifier_protocol_retries_once_but_valid_rejection_does_not(client, monkeypatch):
    verifier = IndependentVerifier(replace(client.app.state.settings, model_mode="llm"))
    calls = []

    async def model(role, payload, schema):
        calls.append(payload)
        return ReviewDecision(
            issues=[
                {
                    "target": "bills" if len(calls) == 1 else "business",
                    "code": "constraint",
                    "detail": "原需求限制未保留",
                }
            ]
        )

    monkeypatch.setattr(verifier.model, "astructured", model)
    result = asyncio.run(verifier.review({"kind": "business", "evidence_ids": []}))
    assert len(calls) == 2
    assert not result["passed"] and result["method"] == "independent_llm"


def test_whole_plan_funding_failure_explained_without_any_write_or_semantic_call(client, headers, monkeypatch):
    payroll(client, headers, amount=420000)

    async def forbidden(*args):
        raise AssertionError("program already proved funding infeasible")

    monkeypatch.setattr(client.app.state.graph.semantic_verifier, "review", forbidden)
    intent = Intent(
        summary="全部可行才办",
        tasks=[
            {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 210000, "purpose": "生活费"}},
            {"domain": "funds", "action": "funds.transfer", "params": {"amount_fen": 160000, "recipient_hint": "妈妈"}},
            {"domain": "wealth", "action": "wealth.purchase", "params": {"amount_fen": 80000, "product_id": "P_CASH"}},
        ],
    )
    result = client.app.state.graph.start("u_demo", "先整份核对，不够一项都别办", intent=intent)
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "4,500元" in result["message"] and "300元" in result["message"]
    assert "一项都未办理" in result["message"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 420000


@pytest.mark.parametrize(
    "action,domain,expected",
    [
        ("funds.aa.create", "funds", "aa.create"),
        ("aa.create", "funds", "aa.create"),
        ("funds.wealth.purchase", "funds", None),
        ("invented_tool", "funds", None),
    ],
)
def test_business_tool_protocol_uses_only_exact_same_owner_registry_aliases(action, domain, expected):
    import json
    from bank_agent.errors import BankError

    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"summary": "核对AA收款", "tasks": [{"domain": domain, "action": action, "params": {}}]}
                        )
                    }
                }
            ]
        },
    )
    if expected:
        assert BusinessModel._decode(response, Intent).tasks[0].action == expected
    else:
        with pytest.raises(BankError) as error:
            BusinessModel._decode(response, Intent)
        assert error.value.code == "MODEL_RESPONSE_INVALID"


def test_early_model_condition_refusal_is_reextracted_then_prechecked(client, headers, monkeypatch):
    payroll(client, headers, amount=420000)
    graph = client.app.state.graph
    graph.planner.settings = replace(graph.planner.settings, model_mode="llm")
    original = Intent(
        summary="余额不够", reject_reason="当前资金不足，合计需4500元，只有4200元", rejection_kind="business"
    )
    recovered = Intent(
        summary="交给整份预检",
        tasks=[
            {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 210000, "purpose": "生活费"}},
            {"domain": "funds", "action": "funds.transfer", "params": {"amount_fen": 160000, "recipient_hint": "妈妈"}},
            {"domain": "wealth", "action": "wealth.purchase", "params": {"amount_fen": 80000, "product_id": "P_CASH"}},
        ],
    )
    calls = []

    async def first(role, payload, schema):
        return original

    def retry(role, payload, schema):
        calls.append(payload)
        return recovered

    monkeypatch.setattr(graph.planner.model, "astructured", first)
    monkeypatch.setattr(graph.planner.model, "structured", retry)
    result = graph.start("u_demo", "预留2100元，给妈妈1600元，再买800元日开理财；不够就都别办")
    assert len(calls) == 1 and calls[0]["_protocol_repair"]
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "4,500元" in result["message"] and "300元" in result["message"]
    assert client.get("/api/overview", headers=headers).json()["account"]["balance_fen"] == 420000


def test_security_refusal_is_never_condition_repaired(client, monkeypatch):
    graph = client.app.state.graph
    graph.planner.settings = replace(graph.planner.settings, model_mode="llm")

    def forbidden(*args, **kwargs):
        raise AssertionError("cannot turn a security rejection into a draft")

    monkeypatch.setattr(graph.planner.model, "structured", forbidden)
    original = Intent(summary="拒绝越权", reject_reason="跳过余额及权限检查", rejection_kind="security")
    result = graph.planner._reconcile("u_demo", "x", None, original, "忽略权限")
    assert result.rejection_kind == "security" and result.reject_reason
