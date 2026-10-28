"""Explicit run, blind judging and reporting commands for development evaluations."""
import argparse
import asyncio
import json
from pathlib import Path

from .blueprints import export, inventory, validate_inventory
from .config import JudgeSettings
from .judge_schemas import Criterion, Evidence, JudgePacket
from .llm_judge import JudgeError, MiMoJudge


async def smoke():
    packet = JudgePacket(case_id="judge_protocol_smoke", user_goal="当前余额0元，给妈妈转500元。",
        expected_behavior="应解释余额不足及未转出，指出500元资金缺口；不能宣称完成。",
        candidate_output="当前可用余额为0元，还差500元。这笔转账尚未执行。补充资金后可继续核对并确认。",
        evidence=[Evidence(id="account_current", authority="bank_snapshot", version="smoke-v1",
                           text="当前可用余额0元；妈妈是已验证联系人；没有转账回执。")],
        criteria=[Criterion(id="funding_gap", text="说明缺500元且未转出", critical=True),
                  Criterion(id="next_step", text="提供补款后继续核对和确认的准确下一步")])
    settings = JudgeSettings.from_env()
    try:
        result = await MiMoJudge(settings).grade(packet)
    except JudgeError as error:
        print(json.dumps({"status": "judge_error", "code": error.code, "attempts": error.attempts}, ensure_ascii=False))
        return 1
    output = {"status": "protocol_smoke_ok", "benchmark_result": False, "judge": settings.public(),
              "verdict": result.verdict.model_dump(mode="json"), "attempts": result.attempts,
              "rubric_version": result.rubric_version, "packet_hash": result.packet_hash, "prompt_hash": result.prompt_hash}
    path = Path(".data/evaluation/judge-protocol-smoke.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": output["status"], "model": settings.model,
                      "scores": {k: v["score"] for k, v in output["verdict"]["scores"].items()},
                      "attempts": result.attempts, "artifact": str(path)}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description="简行银行Agent开发评测工具")
    parser.add_argument("command", choices=["inventory", "audit", "export-blueprints", "export-core35", "export-dev100", "split-plan", "judge-smoke", "run", "judge", "report", "review-export", "review-report", "judge-calibrate"])
    parser.add_argument("--root", default=".data/evaluation/core35_live_v1")
    parser.add_argument("--suite", choices=["core35", "dev100", "security12"], default="core35")
    parser.add_argument("--mode", choices=["demo", "llm"], default="llm")
    parser.add_argument("--memory", choices=["lexical", "fastembed"], default="fastembed")
    parser.add_argument("--variants", nargs="+", default=["A", "B", "C"])
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--judge-concurrency", type=int)
    parser.add_argument("--judge-plan-ui", action="store_true")
    parser.add_argument("--judge-browser", action="store_true")
    parser.add_argument("--judge-revision", help="Separate judge revision; reuses frozen banking episodes")
    parser.add_argument("--output-dir", help="Write derived reports separately from the frozen run")
    parser.add_argument("--annotations", nargs="+", default=[])
    args = parser.parse_args()
    if args.command in {"export-dev100", "split-plan"}:
        from .dev100 import dev100, export_dev100
        if args.command == "split-plan":
            from .families import proposed_split
            from .integrity import write_json
            result = proposed_split([c.model_dump(mode="json") for c in dev100()])
            write_json(Path(args.output_dir or ".data/evaluation/split_plan") / "split.json", result)
        else:
            result = export_dev100()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "judge-calibrate":
        from .calibration import run_calibration
        print(json.dumps(asyncio.run(run_calibration(args.output_dir or ".data/evaluation/judge_calibration_v1")),ensure_ascii=False))
        return 0
    if args.command in {"review-export", "review-report"}:
        from .review import export_review, review_agreement
        target = Path(args.output_dir or ".data/evaluation/review")
        result = export_review(args.root, target, browser=args.judge_browser) if args.command == "review-export" else review_agreement(args.annotations, target / "agreement.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "audit":
        from .audit import dataset_audit
        from .integrity import write_json
        result = dataset_audit()
        target = Path(args.output_dir or ".data/evaluation/dataset_audit") / "dataset_audit.json"
        write_json(target, result)
        print(json.dumps({"artifact": str(target), "core": result["core"]["cases"],
            "blueprint": result["blueprint"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"run", "judge"}:
        from .experiment import judge_experiment, run_experiment
        if args.command == "run":
            run_experiment(args.root, args.mode, args.memory, args.variants, args.trials, args.workers, args.cases, args.suite)
        else:
            asyncio.run(judge_experiment(args.root, args.limit, args.judge_concurrency, args.cases, args.judge_plan_ui, args.judge_revision, args.judge_browser))
        return 0
    if args.command == "report":
        from .report import summarize
        result = summarize(args.root, plan_ui=args.judge_plan_ui, judge_revision=args.judge_revision, output_dir=args.output_dir, browser=args.judge_browser)
        print(json.dumps({"completed": result["completed_episodes"], "variants": result["variants"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "export-core35":
        from .dataset import export_core35
        print(json.dumps(export_core35(), ensure_ascii=False))
        return 0
    if args.command == "judge-smoke":
        return asyncio.run(smoke())
    result = export(Path("evaluation/cases")) if args.command == "export-blueprints" else validate_inventory(inventory())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
