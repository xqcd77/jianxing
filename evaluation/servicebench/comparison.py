"""Paired architecture diagnostics; missing judgements remain missing."""
from collections import Counter
from itertools import combinations
import json
from statistics import mean

import numpy as np

from .diagnostics import efficiency
from .scoring import summarize


def _research_reports(episodes):
    found = {}
    for episode in episodes:
        sources = [t.get("response", {}) for t in episode.get("responses", [])]
        sources += list(episode.get("observations", {}).values())
        for source in sources:
            report = source.get("report") if isinstance(source, dict) else None
            if report and report.get("id"):
                found[(episode["case_id"], episode["trial"], report["id"])] = report
    return list(found.values())


def arm_metrics(rows, episodes, expected):
    result = summarize(rows, expected)
    result.pop("groups", None)
    result["hard_checks"] = {
        "passed": sum(r["objective_checks_pass"] for r in rows), "expected": expected,
        "rate": sum(r["objective_checks_pass"] for r in rows)/expected if expected else None}
    result["goal_fulfilled"] = sum(r["goal_fulfilled"] for r in rows)
    result["efficiency"] = efficiency(episodes, [])
    reports = _research_reports(episodes)
    result["research_observed"] = {
        "reports": len(reports), "verified_rule_fallback": sum(r.get("quality") == "verified_rule_fallback" for r in reports),
        "mean_rounds": mean(r["rounds"] for r in reports) if reports else None,
        "mean_unique_evidence": mean(len(r["evidence"]) for r in reports) if reports else None}
    calls = [m for e in episodes for m in e.get("model_calls", [])]
    result["model_calls_by_role"] = dict(Counter(c["role"] for c in calls))
    result["model_call_errors"] = sum(c["status"] != "ok" for c in calls)
    return result


def paired(left, right, family_by_case):
    """Resolved-pair comparison, alongside explicit missing-judgement counts."""
    left_rows = {(right_rows["case_id"], right_rows["trial"]): right_rows for right_rows in left}
    right_rows = {(right_rows["case_id"], right_rows["trial"]): right_rows for right_rows in right}
    common = sorted(left_rows.keys() & right_rows.keys())
    valid = [k for k in common if left_rows[k]["status"] in {"passed", "failed"} and right_rows[k]["status"] in {"passed", "failed"}]
    delta = {k: int(right_rows[k]["status"] == "passed")-int(left_rows[k]["status"] == "passed") for k in valid}
    counts = Counter((left_rows[k]["status"], right_rows[k]["status"]) for k in valid)
    result = {"matched_episodes": len(common), "resolved_pairs": len(valid), "unresolved_pairs_excluded": len(common)-len(valid),
        "both_pass": counts[("passed", "passed")], "left_only_pass": counts[("passed", "failed")],
        "right_only_pass": counts[("failed", "passed")], "both_fail": counts[("failed", "failed")],
        "right_minus_left_pass_rate": mean(delta.values()) if delta else None,
        "quality_differences": {}, "note": "Resolved pairs only; selection from missing judgements can bias this estimate."}
    for dimension in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"):
        values = [right_rows[k]["scores"][dimension]-left_rows[k]["scores"][dimension] for k in common
                  if dimension in right_rows[k]["scores"] and dimension in left_rows[k]["scores"]]
        result["quality_differences"][dimension] = {"n": len(values), "right_minus_left_mean": mean(values) if values else None}
    if delta:
        # Average repeats per task, then resample whole scenario families.
        task = {case: mean(v for (c, _), v in delta.items() if c == case) for case, _ in delta}
        families = sorted({family_by_case[c] for c in task})
        clusters = [[v for c, v in task.items() if family_by_case[c] == family] for family in families]
        rng = np.random.default_rng(20261017)
        samples = [mean(v for index in selection for v in clusters[index])
                   for selection in rng.integers(0, len(clusters), size=(2000, len(clusters)))]
        low, high = np.quantile(samples, [.025, .975])
        result["family_bootstrap_ci95"] = {"interval": [float(low), float(high)], "families": len(families),
            "replicates": 2000, "seed": 20261017, "degenerate": bool(low == high),
            "diagnostic_not_population_inference": True}
    return result


def compare(root, manifest, scenarios, reports):
    research = {s.execution.id for s in scenarios if any(t.kind == "research" for t in s.execution.steps)}
    family = {s.execution.id: s.family for s in scenarios}
    output = {"design": manifest.get("architecture_comparison", {}), "trials": manifest["trials"],
        "cases_per_arm": len(scenarios), "research_cases": sorted(research), "arms": {}, "pairs": {},
        "single_run_not_all3": manifest["trials"] == 1,
        "caveats": ["Internal author-used diagnostic cases; no independent human labels.",
                    "A common LLM constraint parser is retained; single means one research decision role, not one LLM invocation.",
                    "Domain proposals and report mathematics remain deterministic; most ordinary tasks share the same planner.",
                    "Same per-call token cap and episode call ceiling, but actual compute is measured, not equalized.",
                    "One-pass baselines receive full evidence; C may trade retrieval selectivity for extra rounds.",
                    "Judge averages exclude missing measurements; all-task pass denominators retain them.",
                    f"There are {len(research)} explicitly scripted research tasks; an overall difference is not proof of a general architecture advantage."]}
    declared = manifest.get("benchmark_design", {}).get("predeclared_subsets", {})
    if declared:
        known = {s.execution.id for s in scenarios}
        flat = [case for ids in declared.values() for case in ids]
        if len(flat) != len(set(flat)) or set(flat) != known:
            raise ValueError("Predeclared benchmark subsets must partition the frozen cases")
        output["predeclared_subsets"] = declared
    for variant, report in reports.items():
        episodes = []
        for scenario in scenarios:
            for trial in range(1, manifest["trials"]+1):
                path = root/variant/scenario.execution.id/f"trial_{trial}"/"episode.json"
                if path.exists():
                    episodes.append(json.loads(path.read_text(encoding="utf-8")))
        rows = report["rows"]
        output["arms"][variant] = {
            "all": arm_metrics(rows, episodes, len(scenarios)*manifest["trials"]),
            "research": arm_metrics([r for r in rows if r["case_id"] in research],
                [e for e in episodes if e["case_id"] in research], len(research)*manifest["trials"]),
            "domains": report["groups"]["domain"]}
        if "fulfillable_execution_closure" in report:
            output["arms"][variant]["fulfillable_execution_closure"] = report["fulfillable_execution_closure"]
        if declared:
            output["arms"][variant]["predeclared_subsets"] = {
                name: arm_metrics([r for r in rows if r["case_id"] in ids],
                                  [e for e in episodes if e["case_id"] in ids], len(ids)*manifest["trials"])
                for name, ids in declared.items()}
    for left, right in combinations(reports, 2):
        output["pairs"][left+"_vs_"+right] = {
            "all": paired(reports[left]["rows"], reports[right]["rows"], family),
            "research": paired([r for r in reports[left]["rows"] if r["case_id"] in research],
                               [r for r in reports[right]["rows"] if r["case_id"] in research], family)}
        if declared:
            output["pairs"][left+"_vs_"+right]["predeclared_subsets"] = {
                name: paired([r for r in reports[left]["rows"] if r["case_id"] in ids],
                             [r for r in reports[right]["rows"] if r["case_id"] in ids], family)
                for name, ids in declared.items()}
    return output


def markdown(result):
    """Small reviewable report; no candidate text, credentials or private traces."""
    comparison = result["comparison"]
    arms = comparison["arms"]
    names = {"S_AGENT": "Single-Agent（统一研究角色）", "M_AGENT": "Multi-Agent（单轮完整取证）", "C": "Multi-Agent Deep Research（旧版）",
             "SUPERVISOR": "Supervisor＋4个子Agent"}
    variants = list(arms)
    def fmt(value):
        return "未知" if value is None else f"{value:.2f}"
    lines = ["# BankFlowBench 三组架构对照", "", f"每组{comparison['cases_per_arm']}题，{comparison['trials']}轮；单轮不代表All-3可靠性。",
        "", "共用相同业务模型、权限网关、合成账户、记忆策略、核验器及裁判；对照的是研究决策角色与证据策略。",
        "", "| 指标 | "+" | ".join(names[v] for v in variants)+" |", "| --- | "+" | ".join("---:" for _ in variants)+" |"]
    metric_rows = {}
    for v in variants:
        a = arms[v]["all"]
        status = a["expected_handling"]
        values = {"业务与事实硬检查": f"{a['hard_checks']['passed']}/{a['hard_checks']['expected']}",
            "严格规范处理": f"{status['passed']}/{status['expected']}（{status['pass_rate_lower_bound']*100:.2f}%）",
            "明确失败／未决": f"{status['failed']}／{status['pending_or_unresolved']}",
            "用户目标完整兑现": str(a['goal_fulfilled']), "有效裁判": str(a['judge_valid']),
            "业务模型实际请求": str(a['efficiency']['external_model_calls_known']),
            "整题P50／P95秒": fmt(a['efficiency']['episode_wall_ms']['p50']/1000 if a['efficiency']['episode_wall_ms']['p50'] is not None else None)+"／"+fmt(a['efficiency']['episode_wall_ms']['p95']/1000 if a['efficiency']['episode_wall_ms']['p95'] is not None else None)}
        closure = arms[v].get("fulfillable_execution_closure")
        if closure:
            values["可办理任务完整办结"] = f"{closure['passed']}/{closure['expected']}（未决{closure['pending_or_unresolved']}）"
        for dimension in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"):
            score = a["quality_means_judged_only"].get(dimension, {})
            values[dimension] = fmt(score.get("mean"))+f"（n={score.get('n', 0)}）"
        for key, value in values.items():
            metric_rows.setdefault(key, {})[v] = value
    lines += ["| "+key+" | "+" | ".join(values[v] for v in variants)+" |" for key, values in metric_rows.items()]
    lines += ["", "## 预先指定的研究子集", "", "| 组别 | 严格通过 | 有效裁判 | 平均轮数 | 规则回退报告数 |", "| --- | ---: | ---: | ---: | ---: |"]
    for v in variants:
        a = arms[v]["research"]
        lines.append(f"| {names[v]} | {a['expected_handling']['passed']}/{a['expected_handling']['expected']} | {a['judge_valid']} | {fmt(a['research_observed']['mean_rounds'])} | {a['research_observed']['verified_rule_fallback']} |")
    lines += ["", f"按实际入口识别的显式研究子集共{len(comparison['research_cases'])}题。其余查询和办理主要共用同一LLM规划器，不能从总体分数直接推断多研究角色的因果收益。"]
    if comparison.get("predeclared_subsets"):
        lines += ["", "## 冻结前指定的任务子集", "", "下列分类来自冻结清单，独立于实际是否走进研究入口；避免漏进研究入口的失败任务被移出研究分母。",
                  "", "| 子集 | 组别 | 严格通过 | 有效裁判 | 需求覆盖 | 业务正确性 | 证据推理 | 可操作性 | 清晰度 |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        labels = {"general": "基础业务/服务", "coordination": "跨业务协作", "research": "深度研究"}
        for subset, ids in comparison["predeclared_subsets"].items():
            for v in variants:
                a = arms[v]["predeclared_subsets"][subset]
                status = a["expected_handling"]
                scores = a["quality_means_judged_only"]
                values = [fmt(scores.get(d, {}).get("mean"))+f" (n={scores.get(d, {}).get('n', 0)})"
                          for d in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity")]
                lines.append(f"| {labels.get(subset, subset)}（{len(ids)}题） | {names[v]} | {status['passed']}/{status['expected']} | {a['judge_valid']} | "+" | ".join(values)+" |")
        lines += ["", "组均分仅作描述；架构差异使用JSON中各子集的同题配对结果及有效样本数，不直接比较不同有效题组成的均值。"]
    lines += [
        "", "## 解释限制", "", *["- "+note for note in comparison["caveats"]], ""]
    return "\n".join(lines)
