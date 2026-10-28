"""Blind annotation exchange and agreement from submitted labels only."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from .integrity import validate_episode, write_json
from .packets import build_packet
from .schemas import Case
from .rubrics import DIMENSIONS


def cohen_kappa(left, right, *, ordinal=False):
    if len(left) != len(right):
        raise ValueError("Annotations must be paired")
    n = len(left)
    if not n:
        return {"n": 0, "agreement": None, "kappa": None}
    labels = list(range(1, 6)) if ordinal else sorted(set(left) | set(right))
    a, b = Counter(left), Counter(right)
    distance = (lambda x, y: (x - y) ** 2 / 16) if ordinal else (lambda x, y: int(x != y))
    observed = sum(distance(x, y) for x, y in zip(left, right)) / n
    expected = sum(distance(x, y) * a[x] * b[y] for x in labels for y in labels) / n ** 2
    return {"n": n, "agreement": sum(x == y for x, y in zip(left, right)) / n,
            "kappa": 1 - observed / expected if n >= 2 and expected else None,
            "weighting": "quadratic" if ordinal else "unweighted"}


def export_review(root, directory, browser=False):
    root, directory = Path(root), Path(directory)
    if any((directory / (rater + ".jsonl")).exists() for rater in ("rater_a", "rater_b")):
        raise ValueError("Refusing to overwrite annotation files; use a new review directory")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = {c.id: c for c in map(Case.model_validate_json, (root / "frozen_cases.jsonl").read_text(encoding="utf-8").splitlines())}
    key, forms = {}, []
    for path in sorted(root.glob("*/*/trial_*/episode.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_episode(data, path, root, manifest)
        identity = [data["variant"], data["case_id"], data["trial"]]
        sample = hashlib.sha256(json.dumps([manifest["manifest_hash"], identity]).encode()).hexdigest()[:20]
        captured = None
        if browser:
            from .browser import load_browser_capture
            captured = load_browser_capture(path, manifest["manifest_hash"])
        packet = build_packet(cases[data["case_id"]], data, plan_ui=True, browser=captured).model_dump(mode="json")
        packet["case_id"] = sample
        write_json(directory / "packets" / (sample + ".json"), packet)
        key[sample] = {"variant": data["variant"], "case_id": data["case_id"], "trial": data["trial"]}
        forms.append({"sample_id": sample, "manifest_hash": manifest["manifest_hash"], "annotation_source": "pending",
            "safe_success": None, "ratings": dict.fromkeys(DIMENSIONS), "notes": ""})
    write_json(directory / "private_key.json", {"manifest_hash": manifest["manifest_hash"], "samples": key})
    for rater in ("rater_a", "rater_b"):
        path = directory / (rater + ".jsonl")
        if path.exists():
            raise ValueError("Refusing to overwrite annotation files; use a new review directory")
        path.write_text("".join(json.dumps({**form, "rater": rater}, ensure_ascii=False) + "\n" for form in forms), encoding="utf-8")
    return {"samples": len(forms), "human_double_labelled": False, "directory": str(directory),
            "packet_version": "browser-dom-replay-v1" if browser else "plan-ui-v3",
            "instructions": "Reviewers see packets and their own form; do not share private_key.json. Fill labels and set annotation_source=human."}


def review_agreement(paths, output):
    if len(paths) != 2:
        raise ValueError("Exactly two independent rater files are required")
    sets, raters, manifests = [], [], set()
    for path in paths:
        rows, names = {}, set()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["sample_id"] in rows:
                raise ValueError("Duplicate sample annotation")
            if row.get("annotation_source") not in {"pending", "human"}:
                raise ValueError("This workflow accepts declared human annotations only")
            if row.get("safe_success") is not None and type(row["safe_success"]) is not bool:
                raise ValueError("Human outcome must be boolean or null")
            if set(row["ratings"]) != set(DIMENSIONS):
                raise ValueError("Missing or unknown rating dimensions")
            if any(v is not None and (type(v) is not int or not 1 <= v <= 5) for v in row["ratings"].values()):
                raise ValueError("Ratings must be integer 1..5 or null")
            names.add(row["rater"])
            manifests.add(row["manifest_hash"])
            rows[row["sample_id"]] = row
        if len(names) != 1:
            raise ValueError("Each file must contain one identified rater")
        sets.append(rows)
        raters.extend(names)
    if len(set(raters)) != 2 or len(manifests) != 1:
        raise ValueError("Raters must differ and annotate the same frozen experiment")
    common = set(sets[0]) & set(sets[1])
    paired = [(sets[0][s], sets[1][s]) for s in common
              if all(sets[i][s].get("annotation_source") == "human" for i in (0, 1))]
    outcomes = [(a["safe_success"], b["safe_success"]) for a, b in paired if a["safe_success"] is not None and b["safe_success"] is not None]
    result = {"manifest_hash": next(iter(manifests)), "raters": raters, "paired_submitted_samples": len(paired),
        "human_double_labelled": bool(common) and len(outcomes) == len(common),
        "outcome_agreement": cohen_kappa([a for a, b in outcomes], [b for a, b in outcomes]),
        "ratings": {}, "adjudication_required": []}
    for dimension in DIMENSIONS:
        scores = [(a["ratings"][dimension], b["ratings"][dimension]) for a, b in paired
                  if a["ratings"][dimension] is not None and b["ratings"][dimension] is not None]
        result["ratings"][dimension] = cohen_kappa([a for a, b in scores], [b for a, b in scores], ordinal=True)
    for a, b in paired:
        if a["safe_success"] != b["safe_success"] or a["ratings"] != b["ratings"]:
            result["adjudication_required"].append(a["sample_id"])
    result["coverage"] = {"common_samples": len(common), "paired_outcome_labels": len(outcomes),
        "unpaired_sample_ids": sorted(set(sets[0]) ^ set(sets[1]))}
    write_json(output, result)
    return result
