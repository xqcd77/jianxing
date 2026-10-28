"""Frozen Lite58 plus four long-term memory and four context tasks."""
from pathlib import Path

from .schema import Scenario

ROOT = Path(__file__).resolve().parents[1] / 'cases/bankflow_lite66'


def scenarios():
    from .lite58 import scenarios as original
    rows = [Scenario.model_validate_json(line) for line in
            (ROOT / 'private_gold.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(rows) != 66 or len({s.execution.id for s in rows}) != 66:
        raise ValueError('Lite66 requires exactly 66 unique tasks')
    if [r.model_dump(mode='json') for r in rows[:58]] != [r.model_dump(mode='json') for r in original()]:
        raise ValueError('Lite66 must preserve every Lite58 task and gold assertion')
    if [r.execution.id for r in rows[58:]] != [f'memory_l{i:02}' for i in range(1, 5)] + [f'context_u{i:02}' for i in range(1, 5)]:
        raise ValueError('Memory/context extension order changed')
    return rows
