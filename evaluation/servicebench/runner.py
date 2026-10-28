"""Run language tasks against isolated banking sandboxes; never repair a failed SUT."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import random
from pathlib import Path

from bank_agent.config import Settings
from evaluation.config import JudgeSettings
from evaluation.experiment import source_hash
from evaluation.integrity import validate_episode, write_json
from evaluation.judges import RULES_VERSION
from evaluation.runner import Episode, isolated_directory
from evaluation.watchdog import supervise
from evaluation.schemas import VARIANTS

from .suites import VERSIONS, scenarios
from .dataset import audit, digest
from .fixtures import attach, public_snapshot
from .schema import Scenario

COMPARISON_VARIANTS = ("S_AGENT", "M_AGENT", "C", "SUPERVISOR")
INTERVENTIONS = {
    "SUPERVISOR": {"domain_proposals": "one_business_agent_six_rule_services",
        "research_decision_roles": "bills_and_wealth", "independent_semantic_verifier": True,
        "supervisor": "routing_constraints_synthesis", "evidence": "initial_then_bounded_targeted",
        "research_rounds": "configured_maximum", "orchestration_mode": "supervisor"},
    "S_AGENT": {"domain_proposals": "unified_rules", "research_decision_roles": "one_generalist",
                "evidence": "complete_first_pass", "research_rounds": 1},
    "M_AGENT": {"domain_proposals": "six_domain_rules", "research_decision_roles": "bills_and_wealth",
                "evidence": "complete_first_pass", "research_rounds": 1},
    "C": {"domain_proposals": "six_domain_rules", "research_decision_roles": "bills_and_wealth",
          "evidence": "initial_then_bounded_targeted", "research_rounds": "configured_maximum"},
}


class ServiceEpisode(Episode):
    def __init__(self, scenario, *args, **kwargs):
        self.scenario = scenario
        self.public_requests = []
        super().__init__(scenario.execution, *args, **kwargs)

    def open(self):
        super().open()
        attach(self.app, self.scenario.public_fixture, self.public_requests)


def root_path(root):
    path, base = Path(root).resolve(), Path(".data/evaluation").resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError("Use a new child directory below .data/evaluation")
    return path


def freeze(root, selected=(), mode="llm", trials=1, memory="lexical", workers=3, variants=("C",), suite="base64"):
    root = root_path(root)
    if not 1 <= trials <= 3:
        raise ValueError("Trial count must be 1 to 3; never use best-of-three as reliability")
    if not 1 <= workers <= 4:
        raise ValueError("Use 1 to 4 isolated episode workers")
    if not variants or len(set(variants)) != len(variants) or set(variants)-set(COMPARISON_VARIANTS):
        raise ValueError("Use unique variants from S_AGENT, M_AGENT, C, SUPERVISOR")
    rows = scenarios(suite)
    if selected:
        rows = [s for s in rows if s.execution.id in selected]
        if set(selected) != {s.execution.id for s in rows}:
            raise ValueError("Unknown selected case ID")
    if not rows:
        raise ValueError("Empty suite selection")
    settings = Settings.from_env()
    content = {"suite": VERSIONS[suite], "suite_selection": suite, "cases": [s.execution.id for s in rows], "variants": list(variants), "trials": trials,
        "source_hash": source_hash(), "dataset_hash": audit(rows, suite)["dataset_hash"], "mode": mode, "memory": memory,
        "rules_version": RULES_VERSION, "judge": JudgeSettings.from_env().public(), "execution_workers": workers,
        "sut_model": settings.model_name, "sut_thinking": settings.model_thinking,
        "sut_effort": settings.model_reasoning_effort, "sut_max_output_tokens": settings.model_max_tokens,
        "runtime_contract": {name: getattr(settings, name) for name in (
            "model_timeout", "memory_model", "memory_top_k", "memory_context_chars", "max_replans",
            "research_max_rounds", "research_concurrency", "research_timeout", "demo_enabled", "demo_verification_enabled",
            "agent_runtime", "memory_backend", "model_context_tokens", "task_token_budget")},
        "hard_process_timeout_seconds": 660,
        "max_model_calls_per_episode": 16 if suite == 'lite66' or "SUPERVISOR" in variants else 10,
        "source_mode": "synthetic_snapshot_not_live_market", "diagnostic_only": True, "human_labels": 0,
        "public_fixtures_hash": digest({kind: public_snapshot(kind) for kind in {s.public_fixture for s in rows}})}
    if suite == "lite48":
        from .lite48 import design
        selected_ids = {s.execution.id for s in rows}
        metadata = [m for m in design() if m["case_id"] in selected_ids]
        content["benchmark_design"] = {
            "metadata_hash": digest(metadata),
            "difficulty_policy": "four_structural_tiers_not_empirically_calibrated",
            "predeclared_subsets": {track: [m["case_id"] for m in metadata if m["track"] == track]
                                    for track in ("general", "coordination", "research")},
            "selection_not_outcome_conditioned": True,
        }
    if suite == "lite58":
        content["predeclared_subsets"] = {
            "original48": [s.execution.id for s in rows if not s.execution.id.startswith("complex_c")],
            "complex10": [s.execution.id for s in rows if s.execution.id.startswith("complex_c")],
        }
    if suite == 'lite66':
        content['predeclared_subsets'] = {
            'original58': [s.execution.id for s in rows if not s.execution.id.startswith(('memory_l', 'context_u'))],
            'longterm_memory4': [s.execution.id for s in rows if s.execution.id.startswith('memory_l')],
            'context_understanding4': [s.execution.id for s in rows if s.execution.id.startswith('context_u')],
        }
        content['runtime_contract'].update({name: getattr(settings, name) for name in (
            'memory_maintenance_enabled', 'memory_maintenance_interval', 'memory_maintenance_batch', 'memory_active_dialogues')})
        content['comparison_contract'] = {
            'version': 'lite66-whole-system-v1', 'same_call_ceiling': 16,
            'shared': ['business_model', 'provider', 'per_call_context_and_output_limits', 'bank_gateway',
                'initial_fixtures', 'scripted_user_permissions', 'public_tool_capabilities', 'judge_and_rubric'],
            'baseline': 'S_AGENT legacy unified business proposals and one generalist research role; shared current safety/storage foundation',
            'current': 'SUPERVISOR five-role application with RoleReturn, stage packing and background memory policy',
            'scope': 'whole-system comparison, not isolated causal effect of agent count',
            'repetitions': 1, 'no_best_of_or_selective_case_retries': True}
    if tuple(variants) == ("SUPERVISOR",):
        content["architecture_evaluation"] = {
            "version": "supervisor-four-specialists-v1", "application_runtime": "supervisor",
            "core_roles": ["supervisor", "bills_research", "wealth_research", "business", "verifier"],
            "business_services": 6, "interventions": INTERVENTIONS["SUPERVISOR"],
            "trace_scope": "all_model_adapters_including_supervisor_and_independent_verifier",
            "budget_note": "16 calls includes routing, two researchers, synthesis, independent review and repairs; historical arms retain 10",
            "comparison": False,
        }
    elif tuple(variants) != ("C",):
        content["architecture_comparison"] = {
            "version": "bankflow-architecture-v1", "seed": 20261017,
            "definitions": {v: VARIANTS[v] for v in variants},
            "interventions": {v: INTERVENTIONS[v] for v in variants},
            "research_subset": [s.execution.id for s in rows if any(t.kind == "research" for t in s.execution.steps)],
            "shared": ["model_and_per_call_budget", "typed_constraint_parser", "tool_capability_set",
                       "bank_permissions_and_rules", "verifier_and_report_renderer", "memory_strategy",
                       "initial_financial_fixtures", "per_episode_model_call_ceiling"],
            "scope": "single versus split research decision roles; not one total LLM call or unconstrained autonomous agents",
            "actual_compute_equalized": False,
            "evidence_difference_intentional": "one_pass_receives_all_evidence; C_can_retrieve_it_in_bounded_followups",
            "quality_dependency": "verified claims and most report text are deterministic in all arms; role effects may be limited",
        }
    manifest = {**content, "manifest_hash": digest(content)}
    if (root/"manifest.json").exists():
        old = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
        if old["manifest_hash"] != manifest["manifest_hash"]:
            raise ValueError("Dataset, code or configuration changed: use a new run root")
        return root, rows, old
    if root.exists() and any(root.iterdir()):
        raise ValueError("A new run must start in an empty directory")
    root.mkdir(parents=True, exist_ok=True)
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    write_json(root/"manifest.json", manifest)
    (root/"frozen_scenarios.jsonl").write_text("\n".join(s.model_dump_json() for s in rows)+"\n", encoding="utf-8")
    # Store the exact source bytes as well as their hash for later audit.
    import zipfile
    with zipfile.ZipFile(root/"source_snapshot.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("bank_agent", "evaluation", "scripts"):
            for path in sorted(Path(folder).rglob("*")):
                if path.suffix in {".py", ".js", ".cjs", ".css", ".html", ".ts", ".woff2"}:
                    archive.write(path, path.as_posix())
    return root, rows, manifest


def child_run(scenario_data, root, trial, manifest, variant="C"):
    scenario = Scenario.model_validate(scenario_data)
    case = scenario.execution
    path = isolated_directory(root, variant, case.id, trial)
    if manifest.get('suite_selection') == 'lite66':
        from .memory_context_runtime import MemoryContextEpisode, MemoryContextSpecialistEpisode
        episode_type = MemoryContextSpecialistEpisode if 'specialist20_application' in case.tags else MemoryContextEpisode
        episode = episode_type(scenario, variant, path, manifest['mode'], manifest['memory'])
        episode.trace.max_calls = 16
    elif "specialist20_application" in case.tags:
        from .specialist_runtime import SpecialistEpisode
        episode = SpecialistEpisode(scenario, variant, path, manifest["mode"], manifest["memory"])
    else:
        episode = ServiceEpisode(scenario, variant, path, manifest["mode"], manifest["memory"])
    result = episode.run()
    result.update(trial=trial, manifest_hash=manifest["manifest_hash"], public_requests=episode.public_requests,
        public_fixture=scenario.public_fixture, completion_target=scenario.completion_target,
        runtime_config={"model_mode": manifest["mode"], "model_name": manifest["sut_model"]})
    if source_hash() != manifest["source_hash"]:
        result["sut_error"] = "SOURCE_CHANGED_DURING_EPISODE"
        result["rules"]["safe_success"] = False
        write_json(Path(root)/"run_invalidated.json", {"reason": result["sut_error"]})
    write_json(path.parent/"episode.json", result)


def execute_one(scenario, root, trial, manifest, variant="C"):
    target = Path(root)/variant/scenario.execution.id/f"trial_{trial}"/"episode.json"
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        validate_episode(data, target, root, manifest)
        return data
    if source_hash() != manifest["source_hash"]:
        raise ValueError("Run code changed; do not mix versions")
    # A process that timed out or crashed may already have moved money inside its
    # own sandbox. Preserve it for reconciliation; never silently start it again.
    sandbox = target.parent/"sandbox"
    if (sandbox/"bank.sqlite3").exists():
        raise ValueError("An unfinished sandbox already exists; inspect it rather than replaying")
    status = supervise(child_run, (scenario.model_dump(mode="json"), str(root), trial, manifest, variant), 660)
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        validate_episode(data, target, root, manifest)
        return data
    case = scenario.execution
    data = {"case_id": case.id, "variant": variant, "trial": trial, "manifest_hash": manifest["manifest_hash"],
        "mode": manifest["mode"], "sut_error": "EPISODE_HARD_TIMEOUT" if status["expired"] else "EPISODE_PROCESS_FAILED",
        "rules": {"rules_version": RULES_VERSION, "safe_success": False, "outcome_success": False,
                  "checks": [], "policy_failures": [], "not_evaluated": True},
        "requires_reconciliation": True, "sandbox_preserved": True, "observations": {}, "responses": [], "state": {},
        "trace": [], "usage": None, "model_call_count": None, "llm_exercised": False,
        "runtime_config": {"model_mode": manifest["mode"], "model_name": manifest["sut_model"]}}
    write_json(target, data)
    return data


def run(root, selected=(), mode="llm", trials=1, memory="lexical", workers=3, variants=("C",), suite="base64"):
    root, rows, manifest = freeze(root, selected, mode, trials, memory, workers, variants, suite)
    # Threads only supervise separate child processes. No application instance,
    # LangGraph runtime, financial database or monkeypatch is shared by tasks.
    jobs = [(s, trial, variant) for trial in range(1, trials+1) for s in rows for variant in variants]
    if len(variants) > 1:
        random.Random(20261017).shuffle(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute_one, s, root, trial, manifest, variant): (s, trial, variant)
                   for s, trial, variant in jobs}
        for completed, future in enumerate(as_completed(futures), 1):
            s, trial, variant = futures[future]
            data = future.result()
            print(json.dumps({"case": s.execution.id, "variant": variant, "trial": trial, "state_assertions_pass": data["rules"]["safe_success"],
                "completed": completed, "expected": len(jobs), "semantic_judgement": "pending",
                "sut_error": data.get("sut_error")}, ensure_ascii=True), flush=True)
    return root
