"""Frozen Lite48 plus ten observed complex-request regressions; no case reselection."""
import json
from pathlib import Path

from .schema import Scenario

ROOT = Path(__file__).resolve().parents[1] / "cases" / "bankflow_lite58"


def scenarios():
    rows = [Scenario.model_validate_json(line) for line in
            (ROOT / "private_gold.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    from .lite48 import scenarios as original
    old = original()
    if len(rows) != 58 or len({s.execution.id for s in rows}) != 58:
        raise ValueError("Lite58 requires 58 unique tasks")
    if [s.model_dump(mode="json") for s in rows[:48]] != [s.model_dump(mode="json") for s in old]:
        raise ValueError("Lite58 must preserve the entire original Lite48 contract")
    expected = [f"complex_c{i:02}" for i in range(1, 11)]
    if [s.execution.id for s in rows[48:]] != expected:
        raise ValueError("Complex-request extension order changed")
    return rows


def extension_source():
    return json.loads((ROOT / "extension_source.json").read_text(encoding="utf-8"))
