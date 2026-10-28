"""Validate application fixtures and controlled events without consuming API calls."""
from pathlib import Path
from uuid import uuid4

import pytest

from bank_agent.research_schemas import ResearchBrief
from bank_agent.storage import Account, Document, save_document
from evaluation.schemas import Step
from evaluation.servicebench.dataset import audit
from evaluation.servicebench.specialist import scenarios
from evaluation.servicebench.specialist_runtime import SpecialistEpisode
from evaluation.servicebench.suites import scenarios as suite_scenarios


def episode(key):
    row = next(s for s in scenarios() if s.execution.id == "special_"+key.lower())
    return SpecialistEpisode(row, "C", Path(".data/evaluation")/("native-test-"+uuid4().hex), "demo", "lexical")


def test_registry_preserves_base_and_explicit84():
    assert len(suite_scenarios()) == 64
    assert audit(suite="combined84")["cases"] == 84
    assert audit(suite="specialist20")["semantic_requirements"] == 120
    assert audit(suite="specialist20")["measured_business_forms"] == 0


@pytest.mark.parametrize("key", [f"MA{i:02}" for i in range(1, 11)]+[f"DR{i:02}" for i in range(1, 11)])
def test_real_fixture_balances_and_input_isolation(key):
    e = episode(key)
    try:
        e.prepare()
        state = e.state()
        assert state["ledger_sum"] == state["account_sum"] == 0
        assert state["balance_fen"] == e.case.balance_fen
        assert state["mutating_receipt_count"] == 0
        for contact in e.app.state.bank.facts("u_demo")["contacts"]:
            assert {"name", "phone", "aliases", "notes", "verified", "account_id"} <= contact.keys()
        with e.app.state.db.read() as s:
            for source in e.fixture["sources"]:
                document = s.get(Document, source["source_id"])
                if source.get("visibility") == "available_after_clock_and_controller_event":
                    assert document is None
                else:
                    assert document.data == source
                    assert "requirements" not in document.data
                    assert "oracle_calculations" not in document.data
            if key == "DR01":
                assert s.get(Document, "DR01_DETAIL").data["body"]["AUTH49"]["posted_to_ledger"] is False
        if key == "MA10":
            assert len(e.paired_plans) == 2
            assert len(e.authorized) == 2
        if key == "DR08":
            assert state["available_fen"] == 700000
            assert len(state["plans"]) == 1
    finally:
        e.close()


def test_archive_acl_and_freshness_are_real_read_tool_checks():
    e = episode("DR09")
    try:
        e.prepare()
        tools = e.app.state.research.tools
        before = tools.fingerprint("u_demo")
        with e.app.state.db.write() as s:
            save_document(s, "other_user_record", "financial_evidence", "u_wang", {"source_id": "private_wang", "body": {"balance_fen": 123}})
        evidence = tools.read("u_demo", "bills", "account", ResearchBrief(topic="bills", analysis_only=True))
        assert "other_user_record" not in evidence["source_ids"]
        assert "supporting_records" not in evidence["data"]
        assert all("body" not in x and "status" not in x for x in evidence["data"]["supporting_record_index"])
        details = tools.read("u_demo", "bills", "transaction_details", ResearchBrief(topic="bills", analysis_only=True))
        assert all(x.get("source_id") != "private_wang" for x in details["data"]["supporting_records"])
        assert any(x.get("source_id") == "DR09_ORDERS" for x in details["data"]["supporting_records"])
        assert tools.fingerprint("u_demo") == before
        with e.app.state.db.write() as s:
            row = s.get(Document, "DR09_ORDERS")
            row.data = {**row.data, "version": "2"}
        assert tools.fingerprint("u_demo") != before
    finally:
        e.close()


def test_two_authorized_plans_really_overlap_and_recheck_cash():
    e = episode("MA10")
    try:
        e.prepare()
        e.step(Step(kind="paired_submit", label="concurrent_transfer"))
        state = e.state()
        assert state["specialist"]["overlapping_requests"] is True
        assert state["receipts"]["funds.transfer"] == 1
        assert state["specialist"]["purchase_receipts"] == 0
        assert state["available_fen"] == 50000
        assert state["reserved_fen"] == 100000
        assert all(r["response"].get("id") for r in e.responses)
        assert any(t["kind"] == "api" and "/resume" in t.get("path", "") for t in e.trace.events)
    finally:
        e.close()


def test_external_event_is_balanced_and_future_receipt_is_never_fabricated():
    e = episode("MA08")
    try:
        e.prepare()
        e.step(Step(kind="fixture_event", label="external_posting", params={
            "event": "external_debit", "amount_fen": 150000, "at": "2026-10-13T09:05:00+08:00"}))
        assert e.state()["balance_fen"] == 450000
        assert e.state()["ledger_sum"] == 0
        with e.app.state.db.read() as s:
            assert s.get(Document, "MA08_ACCOUNT_V2").data["body"]["balance_fen"] == s.get(Account, "a_demo").balance_fen
    finally:
        e.close()
