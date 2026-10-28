"""Run identity checks independent of the system's business results."""
from pathlib import Path
import json
import re
from uuid import uuid4


def expected_grid(manifest):
    variants, cases, trials = manifest["variants"], manifest["cases"], manifest["trials"]
    if (not variants or not cases or len(set(variants)) != len(variants) or len(set(cases)) != len(cases)
            or type(trials) is not int or not 1 <= trials <= 3):
        raise ValueError("Invalid experiment grid")
    return {(v, c, t) for v in variants for c in cases for t in range(1, trials + 1)}


def validate_episode(data, path, root, manifest):
    if data.get("manifest_hash") != manifest["manifest_hash"]:
        raise ValueError("Episode manifest mismatch")
    identity = data.get("variant"), data.get("case_id"), data.get("trial")
    if type(identity[2]) is not int or identity not in expected_grid(manifest):
        raise ValueError("Episode is outside the manifest grid")
    expected = Path(root) / identity[0] / identity[1] / f"trial_{identity[2]}" / "episode.json"
    if Path(path).resolve() != expected.resolve():
        raise ValueError("Episode identity does not match its directory")
    if type(data.get("rules", {}).get("safe_success")) is not bool:
        raise ValueError("Missing or non-boolean rule outcome")
    if data["rules"]["safe_success"] and data.get("sut_error") is not None:
        raise ValueError("A successful episode cannot also have an unhandled system error")
    if "rules_version" in manifest and data.get("rules", {}).get("rules_version") != manifest["rules_version"]:
        raise ValueError("Episode rule judge differs from the manifest")
    if "mode" in data and "mode" in manifest and data["mode"] != manifest["mode"]:
        raise ValueError("Episode model mode differs from the manifest")
    return identity


def judgment_paths(episode_path, plan_ui=False, revision=None):
    base = Path(episode_path).parent
    if revision is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", revision) or revision in {"con", "prn", "aux", "nul"}:
            raise ValueError("Judge revision must be a safe lowercase identifier")
        base = base / "judgements" / revision
        return base / "result.json", base / "packet.json"
    suffix = "_plan_ui_v3" if plan_ui else ""
    return base / ("judge_result" + suffix + ".json"), base / ("judge_packet" + suffix + ".json")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
