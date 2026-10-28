"""One immutable 58-task run, or versioned remeasurement of frozen outputs."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.integrity import write_json  # noqa: E402
from evaluation.servicebench.capture import capture  # noqa: E402
from evaluation.servicebench.runner import freeze, run  # noqa: E402
from evaluation.servicebench.scoring import summarize  # noqa: E402
from evaluation.servicebench.workflow import judge_run, preflight_judge, report  # noqa: E402
from bank_agent.config import Settings  # noqa: E402
from bank_agent.memory import Embeddings  # noqa: E402

WEIGHTS = {"coverage": 2, "banking_correctness": 2, "evidence_reasoning": 1, "actionability": 1, "clarity": 1}


def compact(rows, expected):
    result = summarize(rows, expected)
    means = result["quality_means_judged_only"]
    result["weighted_quality_5"] = round(sum(means[k]["mean"] * w for k, w in WEIGHTS.items()) / 7, 4) if set(WEIGHTS) <= means.keys() else None
    result["state_checks_passed"] = sum(row["state_assertions_pass"] for row in rows)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".data/evaluation/l58_fix1")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument('--judge-revision', choices=['v8', 'v9', 'v10', 'v11'], default='v9')
    parser.add_argument('--remeasure-only', action='store_true', help='Reuse every frozen business episode; never execute or replace it')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    control = root.with_name(root.name + ("_remeasure_"+args.judge_revision if args.remeasure_only else "_control"))
    control.mkdir(parents=True, exist_ok=True)
    state = {"started_at": datetime.now(timezone.utc).isoformat(), "run_root": str(root), "expected": 58,
             'judge_revision': args.judge_revision, 'remeasure_only': args.remeasure_only,
             "suite": "lite58", "variant": "SUPERVISOR", "trials": 1, "memory": "fastembed"}

    def update(phase, **extra):
        state.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
        write_json(control / "status.json", state)
        print(json.dumps({"phase": phase, **extra}, ensure_ascii=True), flush=True)

    try:
        if not args.remeasure_only:
            execute_application(root, args.workers, update)
        episodes = list(root.glob("SUPERVISOR/*/trial_1/episode.json"))
        if len(episodes) != 58:
            raise RuntimeError("Application episode grid is incomplete")
        update("read_only_ui_replay", episodes=len(episodes))
        captured = capture(root, clean_dom=True, revision='v3' if args.judge_revision in {'v9','v10','v11'} else 'v2')
        update("judge_contract_preflight", capture=captured)
        preflight = preflight_judge(root, browser=True, clean_dom=True, judge_revision=args.judge_revision)
        write_json(root / ('preflight_'+args.judge_revision+'.json'), preflight)
        update("independent_judging", preflight=preflight)
        asyncio.run(judge_run(root, browser=True, clean_dom=True, judge_revision=args.judge_revision, concurrency=2))
        update("summarizing")
        measured = report(root, browser=True, clean_dom=True, judge_revision=args.judge_revision)
        rows = measured["rows"]
        summary = {"suite": measured["suite"], "variant": "SUPERVISOR", "weights": WEIGHTS,
                   'judge_revision': args.judge_revision, 'remeasure_only': args.remeasure_only,
                   "at": datetime.now(timezone.utc).isoformat(), "run_root": str(root),
                   "all58": compact(rows, 58),
                   "original48": compact([r for r in rows if not r["case_id"].startswith("complex_c")], 48),
                   "complex10": compact([r for r in rows if r["case_id"].startswith("complex_c")], 10),
                   "rows": rows, "efficiency": measured["efficiency"],
                   "note": "One trial per task; original measurements preserved; not an unseen benchmark."}
        write_json(root / ('score_summary_'+args.judge_revision+'.json'), summary)
        write_json(control / "summary.json", summary)
        update("completed" if measured["judge_valid"] == 58 else "completed_with_measurement_gaps",
               strict=summary["all58"]["expected_handling"], weighted_quality_5=summary["all58"]["weighted_quality_5"],
               valid_judgements=measured["judge_valid"])
    except BaseException as error:
        update("interrupted" if isinstance(error, KeyboardInterrupt) else "error", error_type=type(error).__name__)
        raise


def execute_application(root, workers, update):
    update("embedding_preflight")
    configured = Settings.from_env()
    embeddings = Embeddings(configured)
    embeddings.encode(["隔离评测模型缓存校验"])
    if embeddings.backend != "fastembed":
        raise RuntimeError("Prepared fastembed weights are required; do not grade a silent lexical fallback")
    _, scenarios, _ = freeze(root, mode="llm", trials=1, memory="fastembed", workers=workers,
                              variants=("SUPERVISOR",), suite="lite58")
    weights = configured.data_dir / "embedding_cache"
    immutable_files = [p for p in weights.rglob("*") if p.is_file() and p.suffix not in {".incomplete", ".lock"} and ".locks" not in p.parts]
    for scenario in scenarios:
        cache = root / "SUPERVISOR" / scenario.execution.id / "trial_1/sandbox/embedding_cache"
        for source in immutable_files:
            target = cache / source.relative_to(weights)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                # Reuse immutable local model bytes, never account or memory data.
                os.link(source.resolve(), target)
    update("embedding_ready", model=embeddings.model_id, isolated_caches=58, immutable_weight_files=len(immutable_files))
    update("application_execution")
    run(root, mode="llm", trials=1, memory="fastembed", workers=workers,
        variants=("SUPERVISOR",), suite="lite58")

if __name__ == "__main__":
    main()
