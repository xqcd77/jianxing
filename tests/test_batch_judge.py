"""Offline Batch contract tests: anonymous mapping, immutable evidence and billing safety."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from evaluation.config import JudgeSettings
from evaluation.integrity import write_json
from evaluation.judges import RULES_VERSION
from evaluation.servicebench import batchjudge as batch
from evaluation.servicebench.judge import Packet
from evaluation.servicebench.runner import freeze


def packet():
    return Packet(user_goal="向用户说明简行助手身份", dialogue=[{"kind": "chat", "text": "你是谁"}],
        expected_behavior="明确说明自己是简行银行助手，回答身份，不把当前问题变成新的办理要求。",
        acceptable_alternatives="接受简洁的同义身份与能力说明，不要求固定表述。", candidate_output="我是简行银行助手。",
        requirements=[{"id": "r" + str(i), "text": "直接回答身份并保留演示能力边界", "required": True, "weight": 1} for i in range(1, 5)],
        evidence=[], evidence_required=False, applicable_dimensions=["coverage", "actionability", "clarity"],
        observation_scope="browser_dom_regions_v3")


def answer(custom_id, content=None, reasoning="PRIVATE_REASONING_MUST_NOT_BE_SAVED"):
    verdict = {"findings": [{"criterion_id": "r" + str(i), "verdict": "met", "candidate_quote": "我是简行银行助手。",
        "evidence_ids": [], "reason": "候选明确给出了助手身份"} for i in range(1, 5)],
        "scores": {d: {"score": 4 if d in packet().applicable_dimensions else None, "reason": "依照本题适用维度核验结果"}
                   for d in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")},
        "failure_modes": [], "confidence": "high", "summary": "身份说明符合本题要求"}
    return {"custom_id": custom_id, "response": {"status_code": 200, "body": {"model": "mimo-v2.6-pro",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
        "choices": [{"finish_reason": "stop", "message": {"content": content if content is not None else json.dumps(verdict, ensure_ascii=False),
                                                                 "reasoning_content": reasoning}}]}}, "error": None}


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setenv("BANK_JUDGE_API_KEY", "offline-test-key")
    root = Path(".data/evaluation") / ("batch-contract-test-" + uuid4().hex)
    _, rows, manifest = freeze(root, ["service_identity"], "demo", 1, "lexical", 1, ["S_AGENT", "M_AGENT", "C"], "lite48")
    for variant in manifest["variants"]:
        path = root / variant / "service_identity" / "trial_1" / "episode.json"
        write_json(path, {"variant": variant, "case_id": "service_identity", "trial": 1,
            "manifest_hash": manifest["manifest_hash"], "mode": "demo", "sut_error": None,
            "rules": {"safe_success": True, "rules_version": RULES_VERSION, "policy_failures": []},
            "state": {"balance_fen": 600000}, "responses": [], "observations": {}})
    monkeypatch.setattr(batch, "build", lambda *args, **kwargs: packet())
    monkeypatch.setattr("evaluation.servicebench.workflow.build", lambda *args, **kwargs: packet())
    return root


class FakeClient:
    def __init__(self, items=None, fail_create=False):
        self.items = items or []
        self.fail_create = fail_create
        self.creates = 0
        self.uploads = 0

    def upload(self, path):
        self.uploads += 1
        return {"id": "file_input"}

    def create(self, file_id, name):
        self.creates += 1
        self.name, self.file_id = name, file_id
        if self.fail_create:
            raise RuntimeError("MIMO_BATCH_TRANSPORT_ERROR")
        return {"id": "batch_1", "status": "validating"}

    def find_created(self, file_id, name):
        assert (file_id, name) == (self.file_id, self.name)
        return {"id": "batch_1", "status": "in_progress"}

    def retrieve(self, batch_id):
        return {"id": batch_id, "status": "completed", "output_file_id": "file_output"}

    def content(self, file_id):
        return ("\n".join(json.dumps(x, ensure_ascii=False) for x in self.items) + "\n").encode()


def test_real_endpoint_auth_and_request_shape():
    seen = []
    def route(request):
        seen.append(request)
        assert request.url.host == "batch-api-cn.xiaomimimo.com"
        assert request.headers["Authorization"] == "Bearer offline-key"
        assert "api-key" not in request.headers
        assert request.url.path == "/v1/batches"
        payload = json.loads(request.content)
        assert payload["endpoint"] == "/v1/chat/completions" and payload["completion_window"] == "24h"
        return httpx.Response(200, json={"id": "batch_1", "status": "validating"})
    client = batch.BatchClient(JudgeSettings(api_key="offline-key"), httpx.MockTransport(route))
    try:
        assert client.create("file_input", "test")["id"] == "batch_1"
    finally:
        client.close()
    assert len(seen) == 1
    with pytest.raises(ValueError):
        batch.BatchClient(JudgeSettings(api_key="offline-key"), base_url="https://api.xiaomimimo.com/v1")


def test_prepare_is_anonymous_reproducible_and_detects_tamper(frozen):
    assert batch.prepare(frozen)["prepared"] == 3
    folder = batch.round_dir(frozen, 1)
    original = (folder / "input.jsonl").read_bytes()
    assert b"S_AGENT" not in original and b"M_AGENT" not in original and b"offline-test-key" not in original
    assert batch.prepare(frozen)["prepared"] == 3
    assert (folder / "input.jsonl").read_bytes() == original
    (folder / "input.jsonl").write_bytes(original + b" ")
    with pytest.raises(ValueError, match="modified"):
        batch.submit(frozen, client=FakeClient())


def test_lost_creation_response_is_reconciled_not_resubmitted(frozen):
    batch.prepare(frozen)
    client = FakeClient(fail_create=True)
    with pytest.raises(RuntimeError):
        batch.submit(frozen, client=client)
    result = batch.submit(frozen, client=client)
    assert result["batch_id"] == "batch_1"
    assert client.creates == client.uploads == 1
    assert batch.submit(frozen, client=client)["status"] == "already_submitted"
    assert client.creates == 1


def test_reverse_output_order_maps_by_custom_id_and_saves_no_reasoning(frozen):
    batch.prepare(frozen)
    refs = batch.records(batch.round_dir(frozen, 1) / "index.jsonl")
    client = FakeClient([answer(r["custom_id"]) for r in reversed(refs)])
    batch.submit(frozen, client=client)
    result = batch.collect(frozen, client=client)
    assert result["valid"] == 3
    for ref in refs:
        verdict = json.loads((frozen / ref["episode_path"]).with_name("judge_dom_v4.json").read_text(encoding="utf-8"))
        assert verdict["custom_id"] == ref["custom_id"]
        assert verdict["status"] == "ok"
    normalized = (batch.round_dir(frozen, 1) / "normalized.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE_REASONING" not in normalized and "reasoning_content" not in normalized
    assert batch.collect(frozen, client=client) == result


def test_duplicate_provider_id_cannot_assign_a_score_to_another_case(frozen):
    batch.prepare(frozen)
    ref = batch.records(batch.round_dir(frozen, 1) / "index.jsonl")[0]
    client = FakeClient([answer(ref["custom_id"]), answer(ref["custom_id"])])
    batch.submit(frozen, client=client)
    with pytest.raises(ValueError, match="duplicate"):
        batch.collect(frozen, client=client)
    assert not list(frozen.glob("*/*/trial_1/judge_dom_v4.json"))


def test_only_invalid_format_enters_one_repair_batch(frozen):
    batch.prepare(frozen)
    refs = batch.records(batch.round_dir(frozen, 1) / "index.jsonl")
    client = FakeClient([answer(r["custom_id"], content="{}" if i == 0 else None) for i, r in enumerate(refs)])
    batch.submit(frozen, client=client)
    first = batch.collect(frozen, client=client)
    assert first["valid"] == 2 and first["format_errors"] == 1
    good_paths = list(frozen.glob("*/*/trial_1/judge_dom_v4.json"))
    before = {p: p.read_bytes() for p in good_paths}
    assert batch.prepare(frozen, 2)["prepared"] == 1
    retry = batch.records(batch.round_dir(frozen, 2) / "index.jsonl")
    client2 = FakeClient([answer(retry[0]["custom_id"])])
    batch.submit(frozen, 2, client2)
    assert batch.collect(frozen, 2, client2)["valid"] == 1
    assert all(p.read_bytes() == original for p, original in before.items())
    repaired = json.loads((frozen / retry[0]["episode_path"]).with_name("judge_dom_v4.json").read_text(encoding="utf-8"))
    assert len(repaired["attempts"]) == 2 and repaired["status"] == "ok"
    with pytest.raises(ValueError):
        batch.round_dir(frozen, 3)


def test_changed_episode_is_not_graded_against_old_batch(frozen):
    batch.prepare(frozen)
    refs = batch.records(batch.round_dir(frozen, 1) / "index.jsonl")
    client = FakeClient([answer(r["custom_id"]) for r in refs])
    batch.submit(frozen, client=client)
    path = frozen / refs[0]["episode_path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state"]["balance_fen"] = 1
    write_json(path, value)
    with pytest.raises(ValueError, match="changed"):
        batch.collect(frozen, client=client)
