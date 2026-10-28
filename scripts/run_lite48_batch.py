"""One resumable local evaluation job, including overnight MiMo Batch collection.

Not an automation or a recurring task. Run with the project Python interpreter.
The run root stays empty until the ordinary runner freezes its manifest.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from evaluation.integrity import write_json
from evaluation.servicebench.batchjudge import collect, prepare, status, submit
from evaluation.servicebench.capture import capture
from evaluation.servicebench.runner import run
from evaluation.servicebench.workflow import report
from evaluation.trace import scrub


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".data/evaluation/bankflow_lite48_batch_20261018_v1")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    base = Path(".data/evaluation").resolve()
    if not root.is_relative_to(base) or root == base:
        raise ValueError("Use a child run directory below .data/evaluation")
    control = root.with_name(root.name + "_control")
    control.mkdir(parents=True, exist_ok=True)
    lock = control / "pipeline.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise RuntimeError("An evaluation pipeline already owns this run; do not launch a duplicate") from None
    import os
    handle.write(str(os.getpid()))
    handle.close()
    state = {"run_root": str(root), "expected_episodes": 144, "suite": "lite48",
             "variants": ["S_AGENT", "M_AGENT", "C"], "judge_transport": "mimo_batch",
             "started_at": datetime.now(timezone.utc).isoformat()}

    def update(phase, **extra):
        state.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
        write_json(control / "status.json", scrub(state))
        print(json.dumps({"phase": phase, **extra}, ensure_ascii=True), flush=True)

    def batch_round(number):
        update("batch_prepare", batch_round=number)
        prepared = prepare(root, number)
        if not prepared["prepared"]:
            return {"valid": 0, "format_errors": 0, "request_errors": 0}
        update("batch_submit", batch_round=number, batch_requests=prepared["prepared"])
        submitted = submit(root, number)
        update("batch_waiting", batch_round=number, batch_id=submitted["batch_id"],
               provider_status=submitted["status"])
        last, interval, read_failures = None, 60, 0
        while True:
            try:
                remote = status(root, number)
                read_failures = 0
            except RuntimeError as exc:
                transient = str(exc) in {"MIMO_BATCH_TRANSPORT_ERROR", "MIMO_BATCH_HTTP_429",
                    "MIMO_BATCH_HTTP_500", "MIMO_BATCH_HTTP_502", "MIMO_BATCH_HTTP_503", "MIMO_BATCH_HTTP_504"}
                read_failures += 1
                if not transient or read_failures > 8:
                    raise
                update("batch_poll_retry", batch_round=number, consecutive_read_errors=read_failures,
                       provider_error=str(exc))
                for _ in range(4):
                    time.sleep(15)
                continue
            compact = {k: remote[k] for k in ("batch_id", "status", "request_counts", "expires_at")}
            # Always update a heartbeat on disk; only changed provider progress
            # is printed, avoiding repetitive logs during the overnight queue.
            if compact != last:
                update("batch_waiting", batch_round=number, batch_id=remote["batch_id"],
                       provider_status=remote["status"], request_counts=remote["request_counts"],
                       expires_at=remote.get("expires_at"))
                last, interval = compact, 60
            else:
                state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                write_json(control / "status.json", scrub(state))
                interval = min(300, interval + 60)
            if remote["status"] in {"completed", "failed", "expired", "cancelled"}:
                update("batch_collect", batch_round=number)
                return collect(root, number)
            # Short increments allow the local process to be stopped promptly.
            for _ in range(interval // 15):
                time.sleep(15)

    try:
        update("native_application_execution")
        run(root, mode="llm", trials=1, memory="lexical", workers=args.workers,
            variants=("S_AGENT", "M_AGENT", "C"), suite="lite48")
        episodes = list(root.glob("*/*/trial_1/episode.json"))
        if len(episodes) != 144:
            raise RuntimeError("Application episode grid is incomplete")
        update("ui_dom_replay", completed_episodes=len(episodes))
        captured = capture(root, clean_dom=True)
        update("ui_dom_ready", capture=captured)
        first = batch_round(1)
        if first.get("format_errors"):
            batch_round(2)
        update("generating_report")
        final = report(root, True, True, "v4")
        valid = sum(r["judge_valid"] for r in final["variants"].values())
        result = {v: {"strict": r["expected_handling"], "quality": r["quality_means_judged_only"],
                      "closure": r["fulfillable_execution_closure"]} for v, r in final["variants"].items()}
        attempts = [a for path in root.glob("*/*/trial_1/judge_dom_v4.json")
                    for a in json.loads(path.read_text(encoding="utf-8")).get("attempts", [])]
        usages = [a["usage"] for a in attempts if isinstance(a.get("usage"), dict)]
        prompt = sum(u.get("prompt_tokens", 0) for u in usages)
        completion = sum(u.get("completion_tokens", 0) for u in usages)
        cached = sum(u.get("prompt_tokens_details", {}).get("cached_tokens", 0) for u in usages)
        cost = {"prompt_tokens_known": prompt, "completion_tokens_known": completion, "cached_tokens_known": cached,
                "usage_records": len(usages), "unknown_usage_attempts": len(attempts)-len(usages),
                "estimated_cny_at_batch_rates": round(((prompt-cached)*1.5+cached*0.0125+completion*3)/1_000_000, 4),
                "estimate_not_provider_invoice": True}
        write_json(control / "summary.json", {"valid_judgements": valid, "expected": 144, "arms": result, "judge_usage": cost})
        update("completed" if valid == 144 else "completed_with_measurement_gaps",
               valid_judgements=valid, report=str(root / "comparison_dom_v4.md"))
    except BaseException as exc:
        update("interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
               error_type=type(exc).__name__, error=str(exc)[:600])
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
