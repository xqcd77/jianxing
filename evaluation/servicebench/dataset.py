from collections import Counter
import hashlib
import json
from pathlib import Path



def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def audit(rows=None, suite="base64"):
    from .suites import VERSIONS, scenarios as suite_scenarios
    rows = rows if rows is not None else suite_scenarios(suite)
    ids = [s.execution.id for s in rows]
    cap = {"combined84": 84, "lite48": 48, "lite58": 58, 'lite66': 66}.get(suite, 70)
    if len(ids) != len(set(ids)) or not 1 <= len(ids) <= cap:
        raise ValueError("Duplicate case IDs or suite exceeds explicitly selected size")
    measured = [step for s in rows for step in s.execution.steps]
    # Similar prompts share families; cases, turns, repeats and calibration probes
    # never silently become independent samples of user tasks.
    return {"suite": VERSIONS[suite], "cases": len(rows), "domains": dict(Counter(s.domain for s in rows)),
        "difficulty": dict(Counter(s.difficulty for s in rows)), "families": len({s.family for s in rows}),
        "resolution_targets": dict(Counter(s.resolution for s in rows)),
        "completion_targets": dict(Counter(s.completion_target for s in rows)),
        "provenance": dict(Counter(s.provenance for s in rows)),
        "public_fixture_cases": sum(s.public_fixture != "none" for s in rows),
        "semantic_requirements": sum(len(s.requirements) for s in rows),
        "user_language_turns": sum(t.kind in {"chat", "research"} for t in measured),
        "measured_business_forms": sum(t.kind == "business" for t in measured),
        "human_labels": 0, "unseen_holdout": False, "diagnostic_only": True,
        "dataset_hash": digest([s.model_dump(mode="json") for s in rows])}


def export(target=None, suite="base64"):
    from .suites import VERSIONS, scenarios as suite_scenarios
    rows = suite_scenarios(suite)
    target = target or ("evaluation/cases/finservicebench64" if suite == "base64" else
                        "evaluation/cases/bankflow_lite48" if suite == "lite48" else f"evaluation/cases/{suite}_application")
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    public, gold = [], []
    for s in rows:
        c = s.execution
        # SUT only receives one scripted message/action at a time, never this gold.
        public.append({"id": c.id, "title": c.title, "domain": s.domain, "difficulty": s.difficulty,
                       "family": s.family, "turns": [step.model_dump(mode="json") for step in c.steps]})
        gold.append(s.model_dump(mode="json"))
    for name, data in (("public.jsonl", public), ("private_gold.jsonl", gold)):
        (root/name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in data)+"\n", encoding="utf-8")
    reviews = [{"case_id": s.execution.id, "reviewer_id": None, "independent_review": True,
        "gold_acceptable": None, "financial_oracle_correction": None, "ambiguity_notes": None,
        "candidate_output": None, "findings": [{"criterion_id": r.id, "verdict": None, "reason": None}
            for r in s.requirements], "quality_scores": None, "adjudicated_by": None} for s in rows]
    (root/"human_review_template.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in reviews)+"\n", encoding="utf-8")
    manifest = audit(rows, suite)
    manifest.update(public_hash=digest(public), gold_hash=digest(gold))
    (root/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    revision_note = ("本版为48题独立精编版，难度是作者依据任务结构的预分层，尚未经参考模型成功率校准。设计依据与保留/删除理由见README.md、selection_audit.json和case_design.jsonl。"
                     if suite == "lite48" else "修订依据及64题审查记录见同目录《样本修订审查_2026-10-16.md》。")
    lines = [f"# BankFlowBench：{len(rows)}题逐题评测卡", "", "版本：`"+VERSIONS[suite]+"`。由项目作者设计的诊断开发集，未经过独立金融专家双标；不是盲测集。",
        "", "本版仅修订样本，尚未运行业务评测或LLM裁判；旧版分数不代表本版表现。"+revision_note,
        "", "每一题是一个完整任务，多个对话轮次不拆成多道题。合成账户、条款和行情只用于受控测试，不是真实用户数据或当前报价。",
        "", "各题使用前置条件中声明的业务时钟，仅按脚本推进。前置条件是评测环境定义；标准答案与评分项不能作为业务模型输入。",
        "", "保留收款人缺失、报价信息不全等有意设计的澄清题；按题目允许的澄清路径判定，不能强迫模型猜测。", "",
        "| 领域 | 题数 |", "| --- | ---: |"]
    lines += [f"| {name} | {count} |" for name, count in manifest["domains"].items()]
    for index, s in enumerate(rows, 1):
        c = s.execution
        level = {"simple": "简单", "medium": "中等", "complex": "复杂", "easy": "简单", "hard": "困难", "expert": "专家"}[s.difficulty]
        lines += ["", f"## {index:02d}. {c.title}（{c.id}）", "", f"领域：{s.domain}；难度：{level}；目标：{s.completion_target}；同源家族：`{s.family}`。",
            "", "前置条件："+s.preconditions, "", "用户与操作脚本：", ""]
        for step in c.steps:
            if step.kind in {"chat", "research", "memory"}:
                lines.append(f"- {step.kind}：{step.text}")
            else:
                lines.append(f"- 测评操作 `{step.kind}`："+json.dumps(step.params, ensure_ascii=False))
        lines += ["", "正确结果与业务依据："+s.oracle, "", "逐题评分点：", ""]
        lines += [f"- `{r.id}`（必需，权重{r.weight}）：{r.text}" for r in s.requirements]
        lines += ["", "可接受替代："+s.acceptable_alternatives,
            "", "失败辨析："+"；".join(s.failure_modes)+"。",
            "", "程序核验："+"；".join(f"{x.source}.{x.path} {x.op} {x.expected}" for x in c.checks)+"。"]
    (root/"逐题评测卡.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return manifest
