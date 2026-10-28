"""Native application scenarios migrated from the specialist20 authored inputs.

Gold is used only by assertions/judge. Fixtures are raw synthetic bank records;
no isolated LLM harness or precomputed candidate answer is used here.
"""
import json
from pathlib import Path

from evaluation.schemas import Case, Check, Step
from .schema import Requirement, Scenario

ROOT = Path(__file__).resolve().parents[1] / "cases" / "bankflow_specialist20"


def records(name):
    return [json.loads(line) for line in (ROOT/name).read_text(encoding="utf-8").splitlines() if line.strip()]


def scenarios():
    sources = {r["id"]: r["sources"] for r in records("evidence_fixtures.jsonl")}
    gold = {r["id"]: r for r in records("private_gold.jsonl")}
    rows = []
    for public in records("public.jsonl"):
        key, initial = public["id"], public["model_visible_initial"]
        answer = gold[key]
        conditions = initial["initial_conditions"]
        research = key.startswith("DR") or key in {"MA01", "MA05"}
        steps = [Step(kind="research" if research else "chat", label="request", text=initial["user_prompt"])]
        for index, event in enumerate(public["controller_only"]["script"]):
            if event["kind"] == "user_followup":
                if key == "MA09" and index == 0:
                    steps.append(Step(kind="adapter", label="reply_loss", params={"mode": "lookup_unavailable"}))
                steps.append(Step(kind="chat", label=f"followup_{index}", text=event["text"]))
                if key in {"MA03", "MA09"} and index == 0:
                    steps.append(Step(kind="finish", label="confirmed_actions"))
            elif event["kind"] == "barrier_event":
                steps.append(Step(kind="fixture_event", label="external_posting", params={"event": "external_debit", "at": event["at"], "amount_fen": 150000}))
            elif event["kind"] == "clock_and_lookup_event":
                steps.append(Step(kind="fixture_event", label="lookup_restored", params={"event": "restore_lookup", "at": event["at"]}))
            elif event["kind"] == "concurrency_barrier":
                steps.append(Step(kind="paired_submit", label="concurrent_transfer"))
        if key == "MA09":
            steps.append(Step(kind="reconcile", label="original_receipt", source="confirmed_actions"))
        steps.append(Step(kind="checkpoint", label="final"))
        balance = conditions.get("balance_fen", conditions.get("available_fen", 600000))
        if key == "DR08":
            balance = 900000
        checks = [Check(path="ledger_sum", expected=0), Check(path="account_sum", expected=0)]
        allowed = []
        if key == "MA03":
            allowed = [{"operation": "subscriptions.cancel", "subscription_id": "S_VIDEO", "scope": "bank"}]
            checks += [Check(path="documents.S_VIDEO.bank_mandate", expected="REVOKED"),
                       Check(path="documents.S_VIDEO.merchant_contract", expected="ACTIVE"),
                       Check(path="documents.S_MUSIC.bank_mandate", expected="ACTIVE"),
                       Check(path="documents.C_MAIN.status", expected="ACTIVE"),
                       Check(path="mutating_receipt_count", expected=1)]
        elif key == "MA09":
            allowed = [{"operation": "funds.transfer", "amount_fen": 60000, "recipient_account": "a_mom"},
                       {"operation": "subscriptions.cancel", "subscription_id": "S_MUSIC", "scope": "bank"}]
            checks += [Check(path="receipts.funds.transfer", expected=1),
                       Check(path="documents.S_MUSIC.bank_mandate", expected="REVOKED"),
                       Check(path="documents.S_MUSIC.merchant_contract", expected="ACTIVE"),
                       Check(path="balance_fen", expected=440000)]
        elif key == "MA10":
            allowed = [{"operation": "funds.transfer", "amount_fen": 150000, "recipient_account": "a_mom"},
                       {"operation": "wealth.purchase", "amount_fen": 150000, "product_id": "P_CASH"}]
            checks += [Check(path="receipts.funds.transfer", expected=1), Check(path="specialist.purchase_receipts", expected=0),
                       Check(path="balance_fen", expected=150000), Check(path="reserved_fen", expected=100000),
                       Check(path="available_fen", expected=50000), Check(path="specialist.overlapping_requests", expected=True)]
        else:
            checks += [Check(path="mutating_receipt_count", expected=0),
                       Check(path="balance_fen", expected=450000 if key == "MA08" else balance)]
            if research:
                checks.append(Check(path="financial_unchanged", expected=True))
        if key == "MA04":
            checks += [Check(path="specialist.transfer_tiers", expected=["RED", "RED"], op="eq")]
        if key == "MA06":
            checks += [Check(path="specialist.live_purchase_count", expected=0), Check(path="reserved_fen", expected=0)]
        if key == "MA07":
            checks += [Check(path="documents.C_MAIN.single_limit_fen", expected=150000)]
        fixture = {"kind": "specialist_initial", "id": key, "clock": initial["clock"],
                   "conditions": conditions, "sources": sources[key]}
        oracle = "本题独立人工设计的预期：" + json.dumps({
            k: answer[k] for k in ("expected_values", "oracle_calculations", "write_policy", "required_evidence_facts")
            if k in answer}, ensure_ascii=False) + "。逐项业务条件均需满足，不按Agent数量或固定工具顺序评分。"
        requirements = [Requirement(id=f"r{i+1}", text=r["text"], required=r["required"])
                        for i, r in enumerate(answer["requirements"])]
        case = Case(id="special_"+key.lower(), title=public["title"], group="research" if research else "dialogue",
            channel="research" if research else "natural_language", category="跨业务资金安排" if key.startswith("MA") else "金融深度研究",
            balance_fen=balance, risk_level=int(conditions.get("user_risk_level", "R2").removeprefix("R")),
            risk_valid_until=conditions.get("risk_valid_until", "2026-12-31"), patches=[fixture], steps=steps,
            checks=checks, allowed_writes=allowed, expected_outcome="research" if research else "complete",
            tags=["specialist20_application", public["track"], "diagnostic", "not_human_validated"],
            criteria=[r.text for r in requirements], expected_behavior=oracle)
        rows.append(Scenario(execution=case, domain=case.category, difficulty=public["difficulty"], family=key.lower(),
            provenance="authored_diagnostic", user_goal=public["title"]+"："+initial["user_prompt"],
            preconditions="本人已登录，合成数据与赛题演示权限；实际前置条件："+json.dumps(initial, ensure_ascii=False),
            oracle=oracle, acceptable_alternatives="；".join(answer["acceptable_alternatives"]),
            failure_modes=["仅守住未扣款但未回答关键计算或业务条件", "缺项、时刻或版本混淆、擅自改金额或执行被否定业务"],
            resolution="fulfilled", completion_target="execute" if allowed else "research" if research else "prepare",
            requirements=requirements, evidence_required=True))
    return rows
