"""Verify the new dataset controller before freezing any paid application run."""
from pathlib import Path

import pytest

from evaluation.servicebench.dataset import audit
from evaluation.servicebench.lite48 import scenarios as lite48
from evaluation.servicebench.lite58 import scenarios as lite58
from evaluation.servicebench.runner import ServiceEpisode


def test_lite58_preserves_original48_and_declares_ten_extra_tasks():
    rows = lite58()
    assert len(rows) == 58
    assert rows[:48] == lite48()
    assert audit(suite="lite58")["cases"] == 58
    assert all("complex10_extension" in s.execution.tags for s in rows[48:])
    assert all(len(s.requirements) >= 4 for s in rows)


@pytest.mark.parametrize("scenario", lite58()[48:], ids=lambda s: s.execution.id)
def test_complex10_controller_and_financial_gold_are_executable(scenario, tmp_path):
    episode = ServiceEpisode(scenario, "SUPERVISOR", tmp_path / scenario.execution.id, "demo", "lexical")
    result = episode.run()
    assert result["sut_error"] is None, result["sut_error"]
    failed = [c for c in result["rules"]["checks"] if not c["passed"]]
    assert not failed, failed
    assert not result["rules"]["policy_failures"]
    assert result["external_model_call_count"] == 0
    assert all("api_key" not in key.lower() for row in result["model_calls"] for key in row)
    assert Path(episode.settings.data_dir).is_relative_to(tmp_path)
