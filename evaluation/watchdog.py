"""Hard process deadline. A timed-out sandbox is preserved, never replayed."""
import multiprocessing as mp
from pathlib import Path
import json
import time

from .integrity import write_json
from .judges import RULES_VERSION


def child_run(task):
    from .runner import run_case
    from .schemas import Case
    case, variant, root, trial, mode, memory, manifest_hash = task
    run_case(Case.model_validate(case), variant, root, trial, mode, memory, manifest_hash=manifest_hash)


def supervise(target, args, seconds):
    process = mp.get_context("spawn").Process(target=target, args=args)
    process.start()
    process.join(seconds)
    expired = process.is_alive()
    if expired:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    return {"expired": expired, "exitcode": process.exitcode}


def guarded_episode(task, timeout=660):
    case, variant, root, trial, mode, memory, manifest_hash = task
    target = Path(root) / variant / case["id"] / f"trial_{trial}" / "episode.json"
    if target.exists():
        raise ValueError("Existing result must be validated and resumed by the coordinator")
    start = time.perf_counter()
    status = supervise(child_run, (task,), timeout)
    if target.exists():
        # Child result is written once, atomically; completion beats a late exit.
        return json.loads(target.read_text(encoding="utf-8"))
    error = "EPISODE_HARD_TIMEOUT" if status["expired"] else "EPISODE_PROCESS_FAILED"
    result = {"case_id": case["id"], "variant": variant, "trial": trial, "manifest_hash": manifest_hash,
        "title": case["title"], "group": case["group"], "channel": case["channel"], "category": case["category"],
        "expected_outcome": case["expected_outcome"], "mode": mode, "sut_error": error,
        "rules": {"rules_version": RULES_VERSION, "safe_success": False, "outcome_success": False,
            "checks": [], "policy_failures": [], "not_evaluated": True},
        "requires_reconciliation": True, "sandbox_preserved": True,
        "observations": {}, "responses": [], "state": {}, "trace": [], "model_calls": [],
        "model_call_count": 0, "model_call_count_unknown": True, "llm_exercised": False,
        "usage": None, "sut_service_ms": None, "sut_wall_ms": round((time.perf_counter() - start) * 1000, 2),
        "memory_backend": memory, "process_exitcode": status["exitcode"]}
    write_json(target, result)
    return result
