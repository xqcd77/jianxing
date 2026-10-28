"""Evaluation must exercise the new runtime and count the independent reviewer."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import httpx

from bank_agent.deep_banking import DeepBankingGraph
from bank_agent.supervisor_banking import SupervisorBankingGraph
from evaluation.servicebench.lite48 import scenarios
from evaluation.servicebench.runner import ServiceEpisode, freeze
from evaluation.servicebench.workflow import load_run, report


def test_supervisor_manifest_is_single_arm_with_all_48_frozen_tasks():
    root = Path(".data/evaluation") / ("test_supervisor_freeze_" + uuid4().hex)
    _, rows, manifest = freeze(root, mode="demo", workers=1, variants=("SUPERVISOR",), suite="lite48")
    assert len(rows) == 48
    assert load_run(root)[1]["variants"] == ["SUPERVISOR"]
    assert manifest["architecture_evaluation"]["application_runtime"] == "supervisor"
    assert len(manifest["architecture_evaluation"]["core_roles"]) == 5
    assert manifest["max_model_calls_per_episode"] == 16
    assert "architecture_comparison" not in manifest
    empty = report(root)
    assert empty["variant"] == "SUPERVISOR"
    assert empty["expected_handling"]["expected"] == 48
    assert empty["expected_handling"]["passed"] == 0


def test_supervisor_episode_traces_verifier_and_preserves_legacy_arm(tmp_path):
    scenario = next(s for s in scenarios() if s.execution.id == "service_identity")
    episode = ServiceEpisode(scenario, "SUPERVISOR", tmp_path/"new", "demo", "lexical")
    legacy = ServiceEpisode(scenario, "C", tmp_path/"old", "demo", "lexical")
    try:
        episode.prepare()
        legacy.open()
        assert type(episode.app.state.graph) is SupervisorBankingGraph
        assert type(legacy.app.state.graph) is DeepBankingGraph
        assert episode.trace.max_calls == 16 and legacy.trace.max_calls == 10
        verifier = episode.app.state.graph.semantic_verifier
        verifier.settings = replace(verifier.settings, model_mode="llm")
        verifier.model.settings = replace(verifier.model.settings, model_api_key="synthetic-test-only")
        verifier.model.http_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "model": "synthetic", "choices": [{"message": {"content": json.dumps({"issues": []})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }))
        result = asyncio.run(verifier.review({"kind": "research", "evidence_ids": []}))
        assert result["passed"]
        assert episode.trace.calls_started == 1
        assert episode.trace.models[0]["role"] == "verifier"
        assert episode.trace.usage()["total_tokens"] == 14
    finally:
        episode.close()
        legacy.close()
