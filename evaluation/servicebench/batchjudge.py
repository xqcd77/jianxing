"""MiMo Batch transport for the unchanged DOM-v4 atomic judge.

No live-chat fallback. Inputs and anonymous IDs are frozen before upload;
provider output order is irrelevant; successful verdicts are never resubmitted.
Only one format-repair batch is allowed, matching format_retries=1.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from evaluation.config import JudgeSettings
from evaluation.integrity import validate_episode, write_json
from evaluation.trace import scrub
from .judge import Packet, Verdict, content_hash, system_prompt
from .packets import build
from .workflow import episode_path, load_run, report


BASE_URL = "https://batch-api-cn.xiaomimimo.com/v1"
TERMINAL = {"completed", "failed", "expired", "cancelled"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def immutable_json(path, value):
    path = Path(path)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Frozen batch artifact changed: " + path.name)
    else:
        write_json(path, value)


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "command.lock"
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError:
        raise ValueError("Another batch command is active; inspect command.lock before recovery") from None
    try:
        handle.write(now())
        handle.close()
        yield
    finally:
        path.unlink(missing_ok=True)


class BatchClient:
    def __init__(self, settings, transport=None, base_url=BASE_URL):
        parsed = urlparse(base_url)
        if (parsed.scheme != "https" or parsed.hostname != "batch-api-cn.xiaomimimo.com"
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.path.rstrip("/") != "/v1" or parsed.query or parsed.fragment):
            raise ValueError("Use the verified official MiMo China Batch endpoint")
        if not settings.api_key:
            raise ValueError("JUDGE_NOT_CONFIGURED")
        self.client = httpx.Client(base_url=base_url, headers={"Authorization": "Bearer " + settings.api_key},
            timeout=httpx.Timeout(120, connect=20), trust_env=False, follow_redirects=False, transport=transport)

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            # Do not echo credential-bearing request objects or provider text.
            raise RuntimeError("MIMO_BATCH_TRANSPORT_ERROR") from None
        if response.status_code not in {200, 201}:
            raise RuntimeError("MIMO_BATCH_HTTP_" + str(response.status_code))
        return response

    def close(self):
        self.client.close()

    def upload(self, path):
        with Path(path).open("rb") as source:
            return self.request("POST", "/files", data={"purpose": "batch"},
                files={"file": (Path(path).name, source, "application/jsonl")}).json()

    def create(self, file_id, name):
        return self.request("POST", "/batches", json={"input_file_id": file_id,
            "endpoint": "/v1/chat/completions", "completion_window": "24h", "name": name}).json()

    def retrieve(self, batch_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", batch_id):
            raise ValueError("Invalid batch identifier")
        return self.request("GET", "/batches/" + batch_id).json()

    def find_created(self, file_id, name):
        # A lost create response must be reconciled, never blindly POSTed again.
        matches, after = [], None
        for _ in range(10):
            page = self.request("GET", "/batches", params={"limit": 100, **({"after": after} if after else {})}).json()
            matches += [b for b in page.get("data", []) if b.get("input_file_id") == file_id
                        and b.get("name") == name]
            if not page.get("has_more"):
                break
            after = page.get("last_id")
            if not after:
                break
        if len(matches) != 1:
            raise RuntimeError("BATCH_CREATE_RESULT_UNKNOWN: reconcile the saved input file/name; no automatic resubmission")
        return matches[0]

    def content(self, file_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise ValueError("Invalid output file identifier")
        return self.request("GET", "/files/" + file_id + "/content").content


def protocol(root, manifest, settings):
    if settings.public() != manifest["judge"]:
        raise ValueError("Judge configuration changed since the run was frozen")
    value = {"judge": settings.public(), "prompt_hash": content_hash(system_prompt(revision="v4")),
             "scope": "dom_v4", "run_manifest_hash": manifest["manifest_hash"],
             "transport": "mimo_batch", "batch_base_url": BASE_URL, "batch_price_fraction": 0.5}
    immutable_json(root / "judge_protocol_dom_v4.json", value)
    return value


def round_dir(root, round_number):
    if round_number not in {1, 2}:
        raise ValueError("Only an initial batch and one format-repair batch are supported")
    return root / "mimo_batch" / ("round_" + str(round_number))


def request_body(packet, settings, notice=""):
    packet = Packet.model_validate(scrub(packet.model_dump(mode="json")))
    return {"model": settings.model, "messages": [
        {"role": "system", "content": system_prompt(packet) + notice},
        {"role": "user", "content": json.dumps(packet.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)}],
        "response_format": {"type": "json_object"}, "stream": False,
        "thinking": {"type": "enabled" if settings.thinking else "disabled"},
        "max_completion_tokens": settings.max_completion_tokens}


def prepare(root, round_number=1):
    root, manifest, scenarios = load_run(root)
    settings = JudgeSettings.from_env()
    if settings.model != "mimo-v2.6-pro":
        raise ValueError("This experiment is frozen to mimo-v2.6-pro")
    if round_number == 2 and settings.format_retries != 1:
        raise ValueError("Format repair is disabled in this frozen configuration")
    folder = round_dir(root, round_number)
    with locked(folder):
        proto = protocol(root, manifest, settings)
        old_errors = {}
        if round_number == 2:
            summary = json.loads((round_dir(root, 1) / "collection.json").read_text(encoding="utf-8"))
            if not summary["terminal"]:
                raise ValueError("Initial batch is not terminal")
            old_errors = {r["custom_id"]: r for r in records(round_dir(root, 1) / "normalized.jsonl")
                          if r["status"] == "format_error"}
            original_index = {x["episode_path"]: x for x in records(round_dir(root, 1) / "index.jsonl")}
        jobs = [(s, v, t) for s in scenarios for v in manifest["variants"] for t in range(1, manifest["trials"] + 1)]
        random.Random(20261018).shuffle(jobs)
        index, requests = [], []
        for scenario, variant, trial in jobs:
            path = episode_path(root, scenario, trial, variant)
            if not path.exists():
                raise ValueError("All frozen episodes must finish before preparing Batch")
            result = json.loads(path.read_text(encoding="utf-8"))
            validate_episode(result, path, root, manifest)
            packet = build(scenario, result, path, True, True, "v4")
            relative = path.relative_to(root).as_posix()
            notice, previous = "", None
            if round_number == 2:
                previous = original_index[relative]
                if previous["custom_id"] not in old_errors:
                    continue
                if path.with_name("judge_dom_v4.json").exists():
                    raise ValueError("A finalized verdict cannot enter a repair batch")
                notice = ("\n上次输出未通过结构或逐字引用校验。请重新核对本题，严格按JSON schema输出；"
                          "candidate_quote逐字复制候选原文，全部原子项须覆盖，不适用维度仍保留score:null和reason。")
            elif path.with_name("judge_dom_v4.json").exists() and not (folder / "index.jsonl").exists():
                raise ValueError("Do not mix an existing judge result into a new batch")
            packet_data = scrub(packet.model_dump(mode="json"))
            immutable_json(path.with_name("packet_dom_v4.json"), packet_data)
            identity = content_hash({"episode": sha(path), "packet": content_hash(packet_data), "round": round_number})
            custom_id = "bf_" + str(len(index) + 1).zfill(4) + "_" + identity[:12]
            index.append({"custom_id": custom_id, "episode_path": relative,
                "episode_hash": sha(path), "packet_hash": content_hash(packet_data),
                "prompt_hash": content_hash(system_prompt(packet)), "previous_custom_id": previous["custom_id"] if previous else None})
            requests.append({"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions",
                             "body": request_body(packet, settings, notice)})
        for name, rows in (("input.jsonl", requests), ("index.jsonl", index)):
            data = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
            path = folder / name
            if path.exists() and path.read_text(encoding="utf-8") != data:
                raise ValueError("Frozen batch input changed")
            if not path.exists():
                path.write_text(data, encoding="utf-8")
        value = {"round": round_number, "requests": len(index), "input_sha256": sha(folder / "input.jsonl"),
                 "index_sha256": sha(folder / "index.jsonl"), "protocol": proto}
        immutable_json(folder / "manifest.json", value)
    return {"prepared": len(index), "round": round_number, "input_bytes": (folder / "input.jsonl").stat().st_size}


def load_batch(root, round_number):
    root, run, _ = load_run(root)
    folder = round_dir(root, round_number)
    info = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if info["protocol"]["run_manifest_hash"] != run["manifest_hash"]:
        raise ValueError("Batch belongs to a different application run")
    if json.loads((root / "judge_protocol_dom_v4.json").read_text(encoding="utf-8")) != info["protocol"]:
        raise ValueError("Frozen batch judge protocol changed")
    for name, key in (("input.jsonl", "input_sha256"), ("index.jsonl", "index_sha256")):
        if sha(folder / name) != info[key]:
            raise ValueError("Batch input/index was modified")
    if JudgeSettings.from_env().public() != info["protocol"]["judge"]:
        raise ValueError("Frozen judge configuration changed")
    return root, folder, info


def submit(root, round_number=1, client=None):
    root, folder, info = load_batch(root, round_number)
    if not info["requests"]:
        return {"requests": 0, "status": "nothing_to_submit"}
    own = client is None
    client = client or BatchClient(JudgeSettings.from_env())
    try:
        with locked(folder):
            target = folder / "submission.json"
            state = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            if state.get("batch_id"):
                return {"batch_id": state["batch_id"], "status": "already_submitted", "requests": info["requests"]}
            if not state.get("input_file_id"):
                upload = client.upload(folder / "input.jsonl")
                state = {"input_file_id": upload["id"], "uploaded_at": now(),
                         "name": "bankflow-" + info["input_sha256"][:20] + "-r" + str(round_number)}
                write_json(target, state)
            if state.get("create_started_at"):
                batch = client.find_created(state["input_file_id"], state["name"])
            else:
                state["create_started_at"] = now()
                write_json(target, state)
                batch = client.create(state["input_file_id"], state["name"])
            state.update(batch_id=batch["id"], submitted_at=now())
            write_json(target, state)
            write_json(folder / "last_status.json", scrub(batch))
            return {"batch_id": batch["id"], "status": batch.get("status"), "requests": info["requests"]}
    finally:
        if own:
            client.close()


def status(root, round_number=1, client=None):
    _, folder, _ = load_batch(root, round_number)
    submitted = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
    own = client is None
    client = client or BatchClient(JudgeSettings.from_env())
    try:
        value = client.retrieve(submitted["batch_id"])
        write_json(folder / "last_status.json", scrub(value))
        return {"batch_id": value["id"], "status": str(value.get("status", "unknown")).lower(),
                "request_counts": value.get("request_counts", {}), "expires_at": value.get("expires_at")}
    finally:
        if own:
            client.close()


def normalize(item, packet, round_number):
    """Persist verdict/usage only; provider reasoning and invalid raw output stay out."""
    response = item.get("response") or {}
    body = response.get("body") or {}
    attempt = {"attempt": round_number, "usage": scrub(body.get("usage")),
               "provider_model": body.get("model"), "http_status": response.get("status_code"),
               "duration_ms": None, "transport": "mimo_batch"}
    result = {"custom_id": item["custom_id"], "attempt": attempt}
    if item.get("error") or response.get("status_code") != 200:
        attempt["status"] = "batch_request_error"
        return {**result, "status": "request_error", "code": "JUDGE_BATCH_REQUEST_FAILED"}
    try:
        choice = body["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
            raise ValueError("Incomplete response")
        verdict = Verdict.model_validate_json(choice["message"]["content"]).validate_against(packet)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        attempt["status"] = "format_error"
        attempt["validation_error"] = ([{"loc": list(e["loc"]), "type": e["type"]} for e in
            exc.errors(include_input=False, include_context=False, include_url=False)[:8]]
            if isinstance(exc, ValidationError) else type(exc).__name__)
        return {**result, "status": "format_error", "code": "JUDGE_OUTPUT_INVALID"}
    attempt["status"] = "ok"
    return {**result, "status": "ok", "verdict": verdict.model_dump(mode="json")}


def collect(root, round_number=1, client=None):
    root, folder, info = load_batch(root, round_number)
    own = client is None
    client = client or BatchClient(JudgeSettings.from_env())
    try:
        with locked(folder):
            summary_path = folder / "collection.json"
            if summary_path.exists() and json.loads(summary_path.read_text(encoding="utf-8")).get("terminal"):
                return json.loads(summary_path.read_text(encoding="utf-8"))
            submitted = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
            batch = client.retrieve(submitted["batch_id"])
            write_json(folder / "last_status.json", scrub(batch))
            if str(batch.get("status", "")).lower() not in TERMINAL:
                return {"terminal": False, "status": batch.get("status"), "request_counts": batch.get("request_counts", {})}
            index = records(folder / "index.jsonl")
            expected = {x["custom_id"] for x in index}
            received, files = {}, []
            for field in ("output_file_id", "error_file_id"):
                if not batch.get(field):
                    continue
                raw = client.content(batch[field])
                files.append({"kind": field, "file_id": batch[field], "raw_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
                for line in raw.decode("utf-8-sig").splitlines():
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    key = item.get("custom_id")
                    if key not in expected or key in received:
                        raise ValueError("Unknown or duplicate provider custom_id; do not assign scores by line order")
                    received[key] = item
            previous = {r["custom_id"]: r for r in records(round_dir(root, 1) / "normalized.jsonl")} if round_number == 2 else {}
            normalized = []
            for ref in index:
                path = root / ref["episode_path"]
                packet_data = json.loads(path.with_name("packet_dom_v4.json").read_text(encoding="utf-8"))
                if sha(path) != ref["episode_hash"] or content_hash(packet_data) != ref["packet_hash"]:
                    raise ValueError("Candidate or evidence changed after batch submission")
                packet = Packet.model_validate(packet_data)
                item = received.get(ref["custom_id"], {"custom_id": ref["custom_id"], "error": {"code": "result_missing"}})
                checked = normalize(item, packet, round_number)
                normalized.append(checked)
                attempts = ([previous[ref["previous_custom_id"]]["attempt"]] if round_number == 2 else []) + [checked["attempt"]]
                if checked["status"] == "format_error" and round_number == 1 and info["protocol"]["judge"]["format_retries"] == 1:
                    continue  # wait for the one permitted repair; valid results are finalized now
                judgement = {"status": "ok" if checked["status"] == "ok" else "judge_error", "attempts": attempts,
                    "protocol": info["protocol"], "episode_hash": ref["episode_hash"], "packet_hash": ref["packet_hash"],
                    "prompt_hash": ref["prompt_hash"], "rubric_version": "finservice-atomic-judge-v4-schema",
                    "batch_id": batch["id"], "custom_id": ref["custom_id"], "batch_round": round_number}
                if checked["status"] == "ok":
                    judgement["verdict"] = checked["verdict"]
                else:
                    judgement["code"] = checked["code"]
                immutable_json(path.with_name("judge_dom_v4.json"), judgement)
            data = "\n".join(json.dumps(row, ensure_ascii=False) for row in normalized) + "\n"
            (folder / "normalized.jsonl").write_text(data, encoding="utf-8")
            summary = {"terminal": True, "status": str(batch.get("status")).lower(), "batch_id": batch["id"],
                "round": round_number, "expected": len(index), "received": len(received), "valid": sum(r["status"] == "ok" for r in normalized),
                "format_errors": sum(r["status"] == "format_error" for r in normalized),
                "request_errors": sum(r["status"] == "request_error" for r in normalized),
                "files": files, "collected_at": now(), "reasoning_content_saved": False}
            write_json(summary_path, summary)
            report(root, True, True, "v4")
            return summary
    finally:
        if own:
            client.close()


def main():
    parser = argparse.ArgumentParser(description="MiMo Batch DOM-v4 judge; no realtime fallback")
    parser.add_argument("command", choices=["prepare", "submit", "status", "collect"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--round", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    fn = {"prepare": prepare, "submit": submit, "status": status, "collect": collect}[args.command]
    print(json.dumps(fn(args.root, args.round), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
