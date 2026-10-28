"""Load the self-contained Lite48 snapshot, without rebuilding or reselecting 84 cases.

The private contract is consumed only by the controller and scorer. The bank
model still receives one real request and authorized tool evidence at a time.
"""
import json
from pathlib import Path

from .schema import Scenario


ROOT = Path(__file__).resolve().parents[1] / "cases" / "bankflow_lite48"


def design():
    return [json.loads(line) for line in (ROOT / "case_design.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def scenarios():
    rows = [Scenario.model_validate_json(line) for line in
            (ROOT / "private_gold.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 48 or len({s.execution.id for s in rows}) != 48:
        raise ValueError("Lite48 requires exactly 48 unique frozen tasks")
    metadata = design()
    if [m["case_id"] for m in metadata] != [s.execution.id for s in rows]:
        raise ValueError("Lite48 design metadata does not match its frozen task order")
    if any(s.difficulty not in {"easy", "medium", "hard", "expert"} for s in rows):
        raise ValueError("Lite48 requires the four-tier difficulty taxonomy")
    return rows
