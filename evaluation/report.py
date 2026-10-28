"""Report rule outcomes separately from blind language-model quality scores."""
from collections import Counter
from itertools import combinations
import json
from pathlib import Path
import random
import statistics
import hashlib

from .statistics import mcnemar_exact, paired_bootstrap, paired_dz, wilson_interval
from .integrity import expected_grid, validate_episode, judgment_paths
from .judge_schemas import JudgePacket, JudgeVerdict
from .llm_judge import packet_hash


def summarize(root, plan_ui=False, judge_revision=None, output_dir=None, browser=False):
    root = Path(root)
    if (root / "run_invalidated.json").exists():
        raise ValueError("Run was invalidated by a source change; use raw artifacts only for diagnosis")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    output = Path(output_dir) if output_dir else root
    output.mkdir(parents=True, exist_ok=True)
    judge_config = manifest["judge"]
    packet_version = "browser-dom-replay-v1" if browser else "plan-ui-v3" if plan_ui else "api-v1"
    if judge_revision is not None:
        judgment_paths(root / "episode.json", plan_ui, judge_revision)
        contract = json.loads((root / "judge_runs" / judge_revision / "manifest.json").read_text(encoding="utf-8"))
        if (contract["parent_manifest_hash"] != manifest["manifest_hash"]
                or contract["packet_version"] != packet_version):
            raise ValueError("Judge revision or packet mode does not match this report")
        judge_config = contract["judge"]
    grid = expected_grid(manifest)
    frozen = root / "frozen_cases.jsonl"
    cases = [json.loads(line) for line in frozen.read_text(encoding="utf-8").splitlines()] if frozen.exists() else []
    metadata = {c["id"]: c for c in cases}
    episodes, judges = [], {}
    seen = set()
    for path in sorted(root.glob("*/*/trial_*/episode.json")):
        e = json.loads(path.read_text(encoding="utf-8"))
        identity = validate_episode(e, path, root, manifest)
        if identity in seen:
            raise ValueError("Duplicate episode")
        seen.add(identity)
        episodes.append(e)
        jp, packet_path = judgment_paths(path, plan_ui, judge_revision)
        if jp.exists():
            j = json.loads(jp.read_text(encoding="utf-8"))
            if j.get("manifest_hash") != manifest["manifest_hash"]:
                raise ValueError("Judge manifest mismatch")
            if j.get("packet_version", "api-v1") != packet_version:
                raise ValueError("Mixed judge packet versions")
            if j["status"] == "ok":
                packet = JudgePacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
                if j.get("packet_hash") != packet_hash(packet):
                    raise ValueError("Judge packet changed after scoring")
                JudgeVerdict.model_validate(j["verdict"]).validate_against(packet)
            judges[(e["variant"], e["case_id"], e["trial"])] = j
    summaries = {}
    for variant in manifest["variants"]:
        rows = [e for e in episodes if e["variant"] == variant]
        expected = len(manifest["cases"]) * manifest["trials"]
        complete = len(rows) == expected
        wins = sum(e["rules"]["safe_success"] for e in rows)
        judged = [judges[(variant, e["case_id"], e["trial"])] for e in rows
                  if (variant, e["case_id"], e["trial"]) in judges]
        valid = [j for j in judged if j["status"] == "ok"]
        dimensions = {}
        for j in valid:
            for dimension, score in j["verdict"]["scores"].items():
                dimensions.setdefault(dimension, []).append(score["score"])
        usage = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [(e.get("usage") or {}).get(name) for e in rows]
            usage[name] = sum(v for v in values if isinstance(v, int))
            usage[name + "_missing_episodes"] = sum(v is None for v in values)
        judge_usage = Counter()
        for j in judged:
            for attempt in j.get("attempts", []):
                for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = (attempt.get("usage") or {}).get(name)
                    if isinstance(value, int):
                        judge_usage[name] += value
        by_group = {}
        for group in sorted({e["group"] for e in rows}):
            subset = [e for e in rows if e["group"] == group]
            by_group[group] = {"success": sum(e["rules"]["safe_success"] for e in subset), "total": len(subset)}
        by_channel = {}
        for channel in sorted({e["channel"] for e in rows}):
            subset = [e for e in rows if e["channel"] == channel]
            latencies = [e["sut_wall_ms"] for e in subset if e.get("sut_wall_ms") is not None]
            by_channel[channel] = {"success": sum(e["rules"]["safe_success"] for e in subset), "total": len(subset),
                "llm_exercised": sum(e["llm_exercised"] for e in subset),
                "wall_latency_median_ms": statistics.median(latencies) if latencies else None}
        summaries[variant] = {"completed": len(rows), "success": wins,
            "expected": expected, "missing": expected - len(rows), "complete": complete,
            "success_rate": wins / expected if complete else None,
            "observed_success_rate": wins / len(rows) if rows else None,
            "success_rate_lower_bound": wins / expected,
            "wilson95": wilson_interval(wins, len(rows)) if complete and manifest["trials"] == 1 else None,
            "policy_failure_episodes": sum(bool(e["rules"]["policy_failures"]) for e in rows),
            "sut_errors": dict(Counter(e["sut_error"] for e in rows if e["sut_error"])),
            "llm_exercised": sum(e["llm_exercised"] for e in rows), "model_calls": sum(e["model_call_count"] for e in rows),
            "wall_latency_median_ms": statistics.median([e["sut_wall_ms"] for e in rows if e.get("sut_wall_ms") is not None])
                if any(e.get("sut_wall_ms") is not None for e in rows) else None,
            "by_group": by_group, "by_channel": by_channel, "sut_usage": usage, "judge_usage": dict(judge_usage),
            "judge_valid": len(valid), "judge_errors": dict(Counter(j["status"] for j in judged if j["status"] != "ok")),
            "judge_pending": len(rows) - len(judged),
            "quality_means": {k: statistics.mean(v) for k, v in dimensions.items()}}
    comparisons = {}
    keyed = {(e["variant"], e["case_id"], e["trial"]): e for e in episodes}
    for a, b in combinations(manifest["variants"], 2):
        ids = [cid for cid in manifest["cases"] if all((v, cid, t) in keyed
               for v in (a, b) for t in range(1, manifest["trials"] + 1))]
        if not ids:
            continue
        if len(ids) != len(manifest["cases"]):
            comparisons[a + "_vs_" + b] = {"provisional": True, "paired_cases": len(ids),
                "bootstrap": None, "mcnemar": None, "quality": {}, "skipped_reason": "incomplete_task_pairs"}
            continue
        left = [[int(keyed[a, cid, t]["rules"]["safe_success"]) for t in range(1, manifest["trials"]+1)] for cid in ids]
        right = [[int(keyed[b, cid, t]["rules"]["safe_success"]) for t in range(1, manifest["trials"]+1)] for cid in ids]
        strata = [keyed[a, cid, 1]["group"] for cid in ids]
        comparisons[a + "_vs_" + b] = {"bootstrap": paired_bootstrap(left, right, strata=strata),
            "provisional": len(ids) != len(manifest["cases"]),
            "mcnemar": mcnemar_exact([r[0] for r in left], [r[0] for r in right]) if manifest["trials"] == 1 else None}
        from .families import family_bootstrap, family_id
        comparisons[a + "_vs_" + b]["family_bootstrap"] = family_bootstrap(left, right,
            [family_id(metadata.get(cid, {"id": cid, "category": cid})) for cid in ids])
        quality_ids = [cid for cid in ids if all(judges.get((v, cid, t), {}).get("status") == "ok"
                       for v in (a, b) for t in range(1, manifest["trials"] + 1))]
        quality_comparisons = {}
        eligible = (plan_ui or browser) and len(quality_ids) == len(manifest["cases"])
        for dimension in (("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity", "overall") if eligible else ()):
            if len(quality_ids) < 2:
                continue
            def scores(variant):
                result = []
                for cid in quality_ids:
                    values = []
                    for t in range(1, manifest["trials"] + 1):
                        raw = judges[variant, cid, t]["verdict"]["scores"]
                        values.append(statistics.mean(s["score"] for s in raw.values()) if dimension == "overall" else raw[dimension]["score"])
                    result.append(statistics.mean(values))
                return result
            qs = [keyed[a, cid, 1]["group"] for cid in quality_ids]
            quality_comparisons[dimension] = {"bootstrap": paired_bootstrap(scores(a), scores(b), strata=qs),
                                              "effect_size": paired_dz(scores(a), scores(b))}
        comparisons[a + "_vs_" + b]["quality_valid_paired_cases"] = len(quality_ids)
        comparisons[a + "_vs_" + b]["quality_inference_eligible"] = eligible
        if not eligible:
            comparisons[a + "_vs_" + b]["quality_inference_skipped_reason"] = "incomplete_quality_pairs" if plan_ui or browser else "api_v1_capture_known_incomplete"
        comparisons[a + "_vs_" + b]["quality"] = quality_comparisons
    failed = [{"variant": e["variant"], "case_id": e["case_id"], "trial": e["trial"], "title": e["title"],
        "error": e["sut_error"], "failed_checks": [c for c in e["rules"]["checks"] if not c["passed"]],
        "policy_failures": e["rules"]["policy_failures"], "missing_evidence": e["rules"].get("evidence_tools_missing", [])}
        for e in episodes if not e["rules"]["safe_success"]]
    # All failures + reproducible 20% successful episodes; no claim of completed human annotation.
    successes = [e for e in episodes if e["rules"]["safe_success"]]
    sample = random.Random(20261014).sample(successes, min(len(successes), max(1, (len(successes)+4)//5))) if successes else []
    review = [{"variant": e["variant"], "case_id": e["case_id"], "trial": e["trial"], "reviewed": False}
              for e in [*failed, *sample]]
    suffix = "_judge_" + judge_revision if judge_revision else "_plan_ui_v3" if plan_ui else ""
    queue_path = output / ("human_review_queue" + suffix + ".json")
    if queue_path.exists():
        previous = json.loads(queue_path.read_text(encoding="utf-8"))
        existing = {(r["variant"], r["case_id"], r["trial"]): r for r in previous
                    if r.get("manifest_hash", manifest["manifest_hash"]) == manifest["manifest_hash"]}
        selected = {(r["variant"], r["case_id"], r["trial"]) for r in review}
        review = [{**r, **existing.get((r["variant"], r["case_id"], r["trial"]), {})} for r in review]
        review.extend(r for identity, r in existing.items() if r.get("reviewed") and identity in seen and identity not in selected)
    for row in review:
        row["manifest_hash"] = manifest["manifest_hash"]
    result = {"manifest_hash": manifest["manifest_hash"], "expected_episodes": len(grid),
        "missing_episodes": len(grid - seen), "complete": seen == grid,
        "completed_episodes": len(episodes), "variants": summaries, "paired_comparisons": comparisons, "failures": failed,
        "human_double_labelled": False, "development_only": True,
        "quality_packet_version": packet_version}
    result["judge_revision"] = judge_revision
    result["reporter_version"] = "audit-followup-v2"
    result["reporter_source_hashes"] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("report.py", "metrics.py", "families.py", "statistics.py")}
    result["judge_config"] = judge_config
    result["quality_complete"] = sum(s["judge_valid"] for s in summaries.values()) == len(grid)
    from .metrics import diagnostics
    result["diagnostics"] = diagnostics(episodes, cases, manifest)
    from .families import holm_adjust
    result["business_pairwise_holm_p"] = holm_adjust({key: value["mcnemar"]["p_value"] for key, value in comparisons.items()
        if value.get("mcnemar") is not None})
    rescale = root / "judge_rescale_event.json"
    if rescale.exists():
        result["judge_scheduling_change"] = json.loads(rescale.read_text(encoding="utf-8"))
    (output / ("summary" + suffix + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    queue_path.write_text(json.dumps(review, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = [f"# 简行 {manifest.get('suite', '核心任务')} 开发评测报告", "", f"运行目录：`{root.resolve()}`", "",
        f"完成 {len(episodes)}/{result['expected_episodes']} 次；模型 {manifest['sut_model']}，裁判 {judge_config['model']}。",
        f"运行模式：{manifest.get('mode', '历史清单未记录')}；模型名称为配置值，实际调用数与有效评分覆盖须分别核对。",
        *(["本轮是本地规则演示核验，不是业务大模型或LLM裁判成绩。"] if manifest.get("mode") == "demo" else []),
        "完整性：" + ("全部计划内任务已有结果。" if result["complete"] else "运行未完成，最终通过率暂不发布，配对统计只作过程诊断。"),
        f"质量数据包版本：{result['quality_packet_version']}；实际有效评分覆盖见下表，不将部分评分当作全量成绩。",
        "", "|组别|业务与安全断言通过|安全不变量失败|裁判有效结果|平均质量分（五维均值）|",
        "|---|---:|---:|---:|---:|"]
    for v, s in summaries.items():
        quality = statistics.mean(s["quality_means"].values()) if s["quality_means"] else None
        lines.append(f"|{v}|{s['success']}/{s['completed']}|{s['policy_failure_episodes']}|{s['judge_valid']}|{quality:.2f}|" if quality is not None
                     else f"|{v}|{s['success']}/{s['completed']}|{s['policy_failure_episodes']}|{s['judge_valid']}|暂无|")
    lines += ["", "统计口径：业务成功由独立金额、状态、回执、授权和DAG断言决定，语言评分不覆盖业务判定。",
        "延迟采用初始化完成后的任务墙钟时间，包含异步研究等待和自动演示确认，不含真实用户等待；API耗时另存于原始记录。",
        "缺失token不补零；裁判异常单列，不当作有效低分。配对Bootstrap以题为重采样单位，McNemar仅用于单次二元结果；区间仅描述当前开发题。",
        "", "这是开发集的一次初步试跑，尚未完成双人人工标注，不能作为公开基准或生产安全结论。A/B/C共享银行规则、安全门和工具；不是安全机制消融。",
        ("质量评分对象为真实UI对冻结响应的浏览器可见文字回放，包含桌面和手机视口；回放禁止银行API调用，没有重新执行资金业务，也不等于真实用户完整交互实验。"
         if browser else "质量评分对象为接口采集而非浏览器最终呈现。api-v1已发现资金预览及中间强验证步骤采集遗漏，分数仅用于诊断；plan-ui-v3补入真实返回的计划界面字段与确认更新，仍需人工校准。"),
        "", "失败题目：", ""]
    lines += [f"- {e['variant']} / {e['case_id']}：{e['error'] or '业务断言未通过'}；" +
              "、".join(c["description"] for c in e["failed_checks"]) for e in failed]
    lines += ["", "详细断言、模型调用、裁判五维理由、使用量及配对统计见同目录JSON；人工复核队列包含全部失败及成功结果的20%抽样。"]
    if rescale.exists():
        lines += ["", "裁判执行曾从并发2调整至6，RPM仍为20，模型与评分规则不变，已完成结果保留。最多2次在途请求的返回用量不可取得；token合计为观测用量，不等同于计费总量。"]
    (output / ("report" + suffix + ".md")).write_text("\n".join(lines)+"\n", encoding="utf-8")
    return result
