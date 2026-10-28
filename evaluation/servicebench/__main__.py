import argparse
import asyncio
import json

from .dataset import audit, export
from .workflow import preflight_judge


def main():
    parser = argparse.ArgumentParser(description="BankFlowBench：原生应用与界面评测，支持显式选择冻结题集")
    parser.add_argument("command", choices=["audit", "export", "run", "judge", "report", "capture", "calibrate", "preflight"])
    parser.add_argument("--root", default=".data/evaluation/finservice64_v1")
    parser.add_argument("--suite", choices=["base64", "specialist20", "combined84", "lite48", "lite58", 'lite66'], default="base64")
    parser.add_argument("--mode", choices=["demo", "llm"], default="llm")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--judge-concurrency", type=int)
    parser.add_argument("--memory", choices=["lexical", "fastembed"], default="lexical")
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--variants", nargs="+", choices=["S_AGENT", "M_AGENT", "C", "SUPERVISOR"], default=["C"])
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--clean-dom", action="store_true", help="Use versioned DOM regions and v3 judge protocol; preserve old captures/verdicts")
    parser.add_argument("--judge-revision", choices=["v3","v4","v5","v6","v7","v8","v9","v10","v11",'v12'], default="v3")
    parser.add_argument("--recover-infrastructure",action="store_true",help="One immutable recovery of billing/auth/network measurement errors; never regrade valid or quality-failed verdicts")
    parser.add_argument("--recover-invalid-output",action="store_true",help="Retry only cases with no valid verdict after structural parsing failed; preserve the first attempt record")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit(suite=args.suite)
    elif args.command == "export":
        result = export(suite=args.suite)
    elif args.command == "run":
        from .runner import run
        run(args.root, args.cases, args.mode, args.trials, args.memory, args.workers, args.variants, args.suite)
        return
    elif args.command == "judge":
        from .workflow import judge_run
        asyncio.run(judge_run(args.root, args.browser or args.clean_dom, args.limit, args.judge_concurrency, args.clean_dom,args.judge_revision,args.recover_infrastructure,args.cases,args.recover_invalid_output))
        return
    elif args.command == "report":
        from .workflow import report
        result = report(args.root, args.browser or args.clean_dom, args.clean_dom,args.judge_revision)
        result = {k: v for k, v in result.items() if k != "rows"}
        if "variants" in result:
            result["variants"] = {v: {k: value for k, value in report.items() if k != "rows"}
                                  for v, report in result["variants"].items()}
    elif args.command == "capture":
        from .capture import capture
        result = capture(args.root, args.clean_dom, 'v3' if args.judge_revision in {'v9','v10','v11'} else 'v2')
    elif args.command == "preflight":
        result = preflight_judge(args.root, args.browser or args.clean_dom, args.clean_dom, args.judge_revision)
    else:
        from .calibration import run_calibration
        result = asyncio.run(run_calibration(args.root, args.cases, args.clean_dom, args.judge_concurrency,args.judge_revision))
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
