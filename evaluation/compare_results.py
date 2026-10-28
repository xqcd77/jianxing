"""Recompute the published Lite48 comparison offline, without model/API calls."""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path


DEFAULT_DATA = Path(__file__).parent / "published" / "bankflow_lite48_20261018"
WEIGHTS = {
    "coverage": 2,
    "banking_correctness": 2,
    "evidence_reasoning": 1,
    "actionability": 1,
    "clarity": 1,
}
VARIANTS = (
    ("S_AGENT", "Single-Agent", "single-agent.json"),
    ("SUPERVISOR", "Multi-Agent Deep Research", "multi-agent-deep-research.json"),
)


def rounded(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def summarize_variant(data: dict, expected_id: str, label: str) -> dict:
    rows = data["cases"]
    if data["variant"] != expected_id or len(rows) != 48:
        raise ValueError("Expected the specified variant and exactly 48 cases")
    case_ids = [row["case_id"] for row in rows]
    if len(set(case_ids)) != len(rows):
        raise ValueError("Duplicate case IDs")
    valid_rows = []
    for row in rows:
        if row["status"] not in {"passed", "failed", "unresolved"}:
            raise ValueError("Unknown case status")
        if row["status"] == "passed" and row["objective_checks_pass"] is not True:
            raise ValueError("A strict pass cannot fail the objective checks")
        if row["judge_status"] != "ok":
            if row["status"] == "passed":
                raise ValueError("A strict pass requires a valid judge record")
            continue
        if not {"coverage", "actionability", "clarity"} <= row["scores"].keys():
            raise ValueError("A valid judge record lacks required scores")
        for key, value in row["scores"].items():
            if key not in WEIGHTS or type(value) is not int or not 1 <= value <= 5:
                raise ValueError("Scores must be integers from 1 to 5")
        valid_rows.append(row)
    means = {}
    for key in WEIGHTS:
        scores = [row["scores"][key] for row in valid_rows if key in row["scores"]]
        if not scores:
            raise ValueError(f"No applicable scores for {key}")
        means[key] = {
            "mean": rounded(sum(map(Decimal, scores)) / Decimal(len(scores))),
            "n": len(scores),
        }
    # Use the archived reports' four-decimal dimension means, then weight dimensions.
    # This is a weighted macro mean, not a pooled average of all applicable cells.
    weighted = sum(
        Decimal(str(means[key]["mean"])) * weight for key, weight in WEIGHTS.items()
    ) / sum(WEIGHTS.values())
    return {
        "variant": expected_id,
        "label": label,
        "cases": len(rows),
        "strict_passed": sum(row["status"] == "passed" for row in rows),
        "strict_failed": sum(row["status"] == "failed" for row in rows),
        "unresolved": sum(row["status"] == "unresolved" for row in rows),
        "strict_pass_rate": sum(row["status"] == "passed" for row in rows) / len(rows),
        "weighted_score": rounded(weighted),
        "dimension_means": means,
        "judge_valid": len(valid_rows),
        "judge_error": len(rows) - len(valid_rows),
        "source": data["source"],
    }


def compare(data_dir: Path = DEFAULT_DATA) -> dict:
    protocol = json.loads((data_dir / "protocol.json").read_text(encoding="utf-8"))
    results = []
    expected_ids = None
    for variant_id, label, filename in VARIANTS:
        data = json.loads((data_dir / filename).read_text(encoding="utf-8"))
        if (
            data["suite"] != protocol["suite"]
            or data["dataset_hash"] != protocol["dataset_hash"]
            or data["judge_prompt_hash"] != protocol["judge"]["prompt_hash"]
        ):
            raise ValueError("Suite, dataset or judge prompt mismatch")
        ids = [row["case_id"] for row in data["cases"]]
        if expected_ids is not None and ids != expected_ids:
            raise ValueError("The two variants must contain the same ordered cases")
        expected_ids = ids
        results.append(summarize_variant(data, variant_id, label))
    single, deep = results
    return {
        "schema_version": "bankflow-pair-v1",
        "suite": protocol["suite"],
        "date": protocol["results_updated_at"],
        "domains": protocol["domains"],
        "cases": 48,
        "trials_per_variant": 1,
        "dataset_hash": protocol["dataset_hash"],
        "score_weights": WEIGHTS,
        "weighted_score_definition": "Weighted mean of applicable dimension means; total weight 7",
        "strict_pass_definition": "Objective checks and all required task criteria pass; unresolved stays in denominator",
        "variants": results,
        "deep_research_minus_single": {
            "strict_pass_count": deep["strict_passed"] - single["strict_passed"],
            "strict_pass_percentage_points": rounded(
                Decimal(deep["strict_passed"] - single["strict_passed"]) / 48 * 100
            ),
            "weighted_score": rounded(
                Decimal(str(deep["weighted_score"])) - Decimal(str(single["weighted_score"]))
            ),
        },
        "diagnostic_only": True,
        "unseen_holdout": False,
        "human_labels": 0,
        "weighting_selected_after_execution": True,
        "scope_note": protocol["scope_note"],
        "model_call_caps": protocol["model_call_caps"],
        "equal_budget_comparison": False,
    }


def markdown(summary: dict) -> str:
    lines = [
        "| 架构 | 严格通过率 | 五维加权评分（满分5分） |",
        "| --- | ---: | ---: |",
    ]
    for result in summary["variants"]:
        rate = f'{result["strict_pass_rate"]:.2%}'
        count = f'{result["strict_passed"]}/{result["cases"]}'
        score = f'{result["weighted_score"]:.2f}/5'
        lines.append(f'| {result["label"]} | {rate}（{count}） | {score} |')
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, help="Optional output JSON; no external calls")
    args = parser.parse_args()
    summary = compare(args.data_dir)
    if args.output:
        args.output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(markdown(summary))


if __name__ == "__main__":
    main()
