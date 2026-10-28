"""Bounded paired execution, immutable manifests, result-only resume, central judging."""
import asyncio
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version as package_version, PackageNotFoundError
import json
from pathlib import Path
import random
import platform
import zipfile
from urllib.parse import urlparse

from bank_agent.config import Settings

from .config import JudgeSettings
from .dataset import core35, export_core35
from .llm_judge import JudgeError, MiMoJudge, judge_system_prompt, packet_hash, validate_cached_result
from .packets import build_packet
from .rubrics import JUDGE_PROMPT, RUBRIC_VERSION
from .schemas import Case, VARIANTS, validate_executable
from .integrity import validate_episode, judgment_paths, write_json
from .judges import RULES_VERSION


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_hash():
    paths = sorted([p for p in Path("bank_agent").rglob("*") if p.suffix in {".py", ".js", ".ts", ".html", ".css"}]
                   + list(Path("evaluation").rglob("*.py"))
                   + [p for p in Path("scripts").rglob("*") if p.suffix in {".py", ".cjs"}]
                   + [Path(name) for name in ("pyproject.toml", "requirements.lock.txt", "package.json", "tsconfig.json", "tsconfig.strict.json", "compose.yaml", "Dockerfile")])
    return digest([(p.as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths])


def worker(task):
    from .watchdog import guarded_episode
    case_data, variant, root, trial, mode, memory, manifest_hash = task
    manifest = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    if source_hash() != manifest["source_hash"]:
        write_json(Path(root) / "run_invalidated.json", {"reason": "source_changed_before_episode", "case_id": case_data["id"]})
        raise ValueError("Experiment source changed; preserve results and start a new run")
    case = Case.model_validate(case_data)
    result = guarded_episode(task)
    if source_hash() != manifest["source_hash"]:
        write_json(Path(root) / "run_invalidated.json", {"reason": "source_changed_during_episode", "case_id": case_data["id"]})
        raise ValueError("Experiment source changed during execution; result remains diagnostic only")
    target = Path(root) / variant / case.id / f"trial_{trial}" / "episode.json"
    return {"case_id": case.id, "variant": variant, "trial": trial, "success": result["rules"]["safe_success"],
            "error": result["sut_error"], "model_calls": result["model_call_count"], "path": str(target)}


def create_manifest(root, mode, memory, variants, trials, selected, suite="core35"):
    base = Path(".data/evaluation").resolve()
    root = Path(root).resolve()
    if not root.is_relative_to(base) or root == base:
        raise ValueError("Run root must be a child of .data/evaluation")
    settings, judge = Settings.from_env(), JudgeSettings.from_env()
    if suite == "security12":
        from .attack_pairs import security12
        cases = security12()
    elif suite == "dev100":
        from .dev100 import dev100
        cases = dev100()
    else:
        cases = core35()
    if selected:
        cases = [c for c in cases if c.id in selected]
        if {c.id for c in cases} != set(selected):
            raise ValueError("Unknown case ID")
    if not variants or set(variants) - set(VARIANTS) or len(variants) != len(set(variants)) or not 1 <= trials <= 3:
        raise ValueError("Invalid experiment variants or trials")
    for case in cases:
        validate_executable(case)
    content = {"suite": suite, "development": True, "cases": [c.id for c in cases],
        "dataset_hash": digest([c.model_dump(mode="json") for c in cases]), "source_hash": source_hash(),
        "variants": variants, "variant_descriptions": VARIANTS, "trials": trials, "mode": mode, "memory": memory,
        "sut_model": settings.model_name, "sut_thinking": settings.model_thinking, "sut_effort": settings.model_reasoning_effort,
        "sut_max_output_tokens": settings.model_max_tokens, "max_model_calls_per_episode": 10, "episode_timeout_seconds": 600,
        "hard_process_timeout_seconds": 660,
        "judge": judge.public(), "rubric_version": RUBRIC_VERSION, "rubric_hash": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
        "order_seed": 20261014, "human_double_labelled": False}
    content["rules_version"] = RULES_VERSION
    content["runtime_contract"] = {name: getattr(settings, name) for name in (
        "model_timeout", "memory_model", "memory_top_k", "memory_context_chars", "max_replans",
        "research_max_rounds", "demo_enabled", "demo_verification_enabled")}
    content["runtime_contract"].update({name: getattr(settings, name) for name in (
        "memory_strategy", "research_concurrency", "research_queue_limit", "research_timeout")})
    content["variant_interventions"] = {
        "R_SINGLE": {"research_roles": 1, "execution_graph": "unchanged", "shared_memory": "unchanged"},
        "R_DUAL": {"research_roles": 2, "execution_graph": "unchanged", "shared_memory": "unchanged"},
        "R_ONESHOT": {"research_roles": 2, "evidence_strategy": "one_pass_complete"},
        "M_NONE": {"memory_strategy": "none"}, "M_RAW": {"memory_strategy": "raw"},
        "M_COMPRESSED": {"memory_strategy": "compressed"}}
    endpoint = urlparse(settings.model_base_url)
    content["sut_endpoint_origin"] = {"scheme": endpoint.scheme, "host": endpoint.hostname, "port": endpoint.port}
    content["python_version"] = platform.python_version()
    content["dependencies"] = {}
    for name in ("langgraph", "langgraph-checkpoint-sqlite", "sqlalchemy", "pydantic", "httpx", "numpy", "fastembed"):
        try:
            content["dependencies"][name] = package_version(name)
        except PackageNotFoundError:
            content["dependencies"][name] = None
    path = root / "manifest.json"
    manifest_hash = digest(content)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old["manifest_hash"] != manifest_hash:
            raise ValueError("Manifest changed: use a new run ID instead of mixing configurations")
    else:
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**content, "manifest_hash": manifest_hash,
            "created_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "frozen_cases.jsonl").write_text("\n".join(c.model_dump_json() for c in cases)+"\n", encoding="utf-8")
        snapshot_paths = [p for folder in ("bank_agent", "evaluation", "scripts") for p in Path(folder).rglob("*")
                          if p.suffix in {".py", ".js", ".ts", ".cjs", ".css", ".html"}]
        snapshot_paths += [Path(name) for name in ("pyproject.toml", "requirements.lock.txt", "package.json", "tsconfig.json", "tsconfig.strict.json", "compose.yaml", "Dockerfile")]
        with zipfile.ZipFile(root / "source_snapshot.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(snapshot_paths):
                if source.is_file():
                    archive.write(source, source.as_posix())
    return root, cases, manifest_hash


def run_experiment(root, mode="llm", memory="fastembed", variants=("A", "B", "C"), trials=1, workers=3, selected=(), suite="core35"):
    root, cases, manifest_hash = create_manifest(root, mode, memory, list(variants), trials, selected, suite)
    export_core35()
    tasks, existing = [], 0
    for case in cases:
        for trial in range(1, trials+1):
            for variant in variants:
                path = root / variant / case.id / f"trial_{trial}" / "episode.json"
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    validate_episode(data, path, root, json.loads((root / "manifest.json").read_text(encoding="utf-8")))
                    existing += 1
                else:
                    tasks.append((case.model_dump(mode="json"), variant, str(root), trial, mode, memory, manifest_hash))
    random.Random(20261014).shuffle(tasks)
    print(json.dumps({"stage": "run", "scheduled": len(tasks), "resumed_results": existing, "root": str(root)}, ensure_ascii=True), flush=True)
    if tasks:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            jobs = [pool.submit(worker, task) for task in tasks]
            for index, future in enumerate(as_completed(jobs), 1):
                result = future.result()
                print(json.dumps({"completed": index, "total_new": len(tasks), **result}, ensure_ascii=True), flush=True)
    return root


async def judge_experiment(root, limit=None, concurrency=None, selected=(), plan_ui=False, revision=None, browser=False):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    settings = JudgeSettings.from_env()
    packet_version = "browser-dom-replay-v1" if browser else "plan-ui-v3" if plan_ui else "api-v1"
    if browser and revision is None:
        raise ValueError("Browser capture requires a separate judge revision")
    if revision is None and settings.public() != manifest["judge"]:
        raise ValueError("Judge configuration changed: use a separate judging run")
    if revision is not None:
        judgment_paths(root / "episode.json", plan_ui, revision)  # Validate before constructing any path.
    if concurrency is not None:
        if not 1 <= concurrency <= 6:
            raise ValueError("Judge concurrency must be between 1 and 6")
        settings = replace(settings, concurrency=concurrency)
    event = {"effective_settings": settings.public(), "original_settings": manifest["judge"],
             "packet_version": packet_version,
             "manifest_hash": manifest["manifest_hash"], "started_at": datetime.now(timezone.utc).isoformat(),
             "change_scope": "independent judge revision; banking episodes reused" if revision else
                "scheduling only; same model, thinking, rubric and token limit"}
    execution_log = root / "judge_execution.jsonl"
    with execution_log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    frozen = root / "frozen_cases.jsonl"
    frozen_cases = [Case.model_validate_json(line) for line in frozen.read_text(encoding="utf-8").splitlines()] if frozen.exists() else [c for c in core35() if c.id in manifest["cases"]]
    if digest([c.model_dump(mode="json") for c in frozen_cases]) != manifest["dataset_hash"]:
        raise ValueError("Judge gold differs from the frozen dataset")
    cases = {c.id: c for c in frozen_cases}
    if revision is not None:
        contract = {"parent_manifest_hash": manifest["manifest_hash"], "dataset_hash": manifest["dataset_hash"],
            "judge": {k: v for k, v in settings.public().items() if k not in {"concurrency", "rpm"}},
            "rubric_version": RUBRIC_VERSION, "rubric_hash": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
            "prompt_hash": hashlib.sha256(judge_system_prompt().encode()).hexdigest(),
            "packet_version": packet_version,
            "packet_builder_hash": hashlib.sha256(Path(__file__).with_name("packets.py").read_bytes()).hexdigest(),
            "ui_labels_hash": hashlib.sha256(Path("bank_agent/static/ui.js").read_bytes()).hexdigest() if plan_ui else None}
        contract_path = root / "judge_runs" / revision / "manifest.json"
        if browser:
            from .browser import ui_source_hash
            contract["ui_source_hash"] = ui_source_hash()
        if contract_path.exists() and json.loads(contract_path.read_text(encoding="utf-8")) != contract:
            raise ValueError("Judge revision contract changed; use a new revision identifier")
        if not contract_path.exists():
            write_json(contract_path, contract)
    model = MiMoJudge(settings)
    paths = sorted(root.glob("*/*/trial_*/episode.json"))
    if selected:
        if set(selected) - set(cases):
            raise ValueError("Unknown judging case")
        paths = [p for p in paths if p.parent.parent.name in selected]
    if limit:
        paths = paths[:limit]
    complete = 0
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def one(path):
        nonlocal complete
        target, packet_path = judgment_paths(path, plan_ui, revision)
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_episode(data, path, root, manifest)
        case = cases[data["case_id"]]
        captured = None
        if browser:
            from .browser import load_browser_capture
            captured = load_browser_capture(path, manifest["manifest_hash"])
        if target.exists():
            packet = build_packet(case, data, plan_ui=plan_ui, **({"browser": captured} if browser else {}))
            validate_cached_result(json.loads(target.read_text(encoding="utf-8")), packet,
                                   manifest["manifest_hash"], packet_version)
            return
        async with semaphore:
            packet = None
            try:
                packet = build_packet(case, data, plan_ui=plan_ui, **({"browser": captured} if browser else {}))
                write_json(packet_path, packet.model_dump(mode="json"))
                result = await model.grade(packet)
                output = {"status": "ok", "verdict": result.verdict.model_dump(mode="json"),
                    "attempts": result.attempts, "packet_hash": result.packet_hash, "prompt_hash": result.prompt_hash,
                    "rubric_version": result.rubric_version, "manifest_hash": manifest["manifest_hash"]}
            except JudgeError as e:
                output = {"status": "judge_error", "code": e.code, "attempts": e.attempts,
                          "manifest_hash": manifest["manifest_hash"]}
            except (ValueError, KeyError) as e:
                output = {"status": "packet_error", "code": type(e).__name__, "attempts": [],
                          "manifest_hash": manifest["manifest_hash"]}
            output["packet_version"] = packet_version
            output.update(case_id=data["case_id"], variant=data["variant"], trial=data["trial"], judge_revision=revision,
                rubric_version=RUBRIC_VERSION, prompt_hash=hashlib.sha256(judge_system_prompt().encode()).hexdigest())
            if packet is not None:
                output["packet_hash"] = packet_hash(packet)
            write_json(target, output)
            complete += 1
            print(json.dumps({"stage": "judge", "completed": complete, "case_id": data["case_id"],
                "variant": data["variant"], "status": output["status"]}, ensure_ascii=True), flush=True)

    await asyncio.gather(*(one(p) for p in paths))
    return root
