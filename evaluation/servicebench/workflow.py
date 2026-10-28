import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import random
from pathlib import Path
from uuid import uuid4

from evaluation.config import JudgeSettings
from evaluation.integrity import validate_episode, write_json
from evaluation.llm_judge import JudgeError

from .dataset import digest
from .judge import ServiceJudge, Verdict, content_hash, strict_verdict_tool, system_prompt, validate_strict_tool
from .packets import build
from .schema import Scenario
from .scoring import assess, summarize
from .diagnostics import efficiency, state_coverage


def load_run(root):
    root = Path(root)
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    if digest({k: v for k, v in manifest.items() if k not in {"created_at", "manifest_hash"}}) != manifest["manifest_hash"]:
        raise ValueError("Frozen run manifest was altered")
    rows = [Scenario.model_validate_json(line) for line in (root/"frozen_scenarios.jsonl").read_text(encoding="utf-8").splitlines()]
    if digest([s.model_dump(mode="json") for s in rows]) != manifest["dataset_hash"]:
        raise ValueError("Frozen task and oracle data changed")
    variants = manifest["variants"]
    if ([s.execution.id for s in rows] != manifest["cases"] or not variants or
            len(set(variants)) != len(variants) or set(variants)-{"C", "S_AGENT", "M_AGENT", "SUPERVISOR"} or
            not 1 <= manifest["trials"] <= 3):
        raise ValueError("Frozen task grid does not match manifest")
    return root, manifest, rows


def episode_path(root, scenario, trial, variant="C"):
    return root/variant/scenario.execution.id/f"trial_{trial}"/"episode.json"


def same_judge_scoring_configuration(reference, effective):
    # Semaphore size changes request scheduling, not the request body or rubric.
    # RPM, timeout, model, thinking and token budget remain fixed.
    return {k: v for k, v in reference.items() if k != "concurrency"} == {
        k: v for k, v in effective.items() if k != "concurrency"}


RECOVERABLE = {"JUDGE_HTTP_401","JUDGE_HTTP_402","JUDGE_HTTP_403","JUDGE_TRANSPORT_ERROR","JUDGE_DEADLINE_EXCEEDED"}
RECOVERABLE |= {"JUDGE_SCHEDULER_STOPPED_"+code for code in list(RECOVERABLE) if code.startswith("JUDGE_HTTP_")}


def measurement_recovery_allowed(old, infrastructure=False, invalid_output=False):
    """Only one recovery of a missing measurement; a valid grade is immutable."""
    if old.get('status')=='ok' or old.get('recovery_attempted') or old.get('recovered_infrastructure') or old.get('recovery_kind'):
        return False
    return bool(infrastructure and old.get('code') in RECOVERABLE
                or invalid_output and old.get('code')=='JUDGE_OUTPUT_INVALID')


def preflight_judge(root, browser=False, clean_dom=False, judge_revision="v3"):
    """Validate the complete frozen judge grid and packet contracts without API calls."""
    root, manifest, scenarios = load_run(root)
    expected = len(scenarios)*manifest["trials"]*len(manifest["variants"])
    seen, max_packet_chars, max_spans = 0, 0, 0
    from .judge import candidate_quote_spans
    for scenario in scenarios:
        for trial in range(1, manifest["trials"]+1):
            for variant in manifest["variants"]:
                path = episode_path(root, scenario, trial, variant)
                if not path.exists():
                    raise ValueError("JUDGE_PREFLIGHT_MISSING_EPISODE:"+str(path.relative_to(root)))
                result = json.loads(path.read_text(encoding="utf-8"))
                validate_episode(result, path, root, manifest)
                packet = build(scenario, result, path, browser, clean_dom, judge_revision)
                serialized = json.dumps(packet.model_dump(mode="json"), ensure_ascii=False)
                max_packet_chars = max(max_packet_chars, len(serialized))
                spans = candidate_quote_spans(packet)
                max_spans = max(max_spans, len(spans))
                if judge_revision in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"} and clean_dom:
                    tool = strict_verdict_tool(packet, judge_revision)
                    validate_strict_tool(tool)
                    schema = tool["function"]["parameters"]
                    if not tool["function"].get("strict") or schema.get("additionalProperties") is not False:
                        raise ValueError("JUDGE_PREFLIGHT_STRICT_SCHEMA_DISABLED")
                    if set(schema["required"]) != set(schema["properties"]):
                        raise ValueError("JUDGE_PREFLIGHT_TOP_LEVEL_REQUIRED_MISMATCH")
                    finding_schema = schema["properties"]["findings"]
                    if judge_revision in {"v7", "v8", "v9", "v10", "v11", "v12"}:
                        finding_item = finding_schema.get("items", {})
                        if (finding_schema.get("type") != "array" or
                                set(finding_item.get("required", [])) != {"criterion_id", "verdict", "candidate_quote_id", "evidence_ids", "reason"} or
                                set(finding_item.get("properties", {}).get("criterion_id", {}).get("enum", [])) != {r["id"] for r in packet.requirements}):
                            raise ValueError("JUDGE_PREFLIGHT_REQUIREMENT_SCHEMA_MISMATCH")
                    else:
                        if (finding_schema.get("type") != "object" or finding_schema.get("additionalProperties") is not False or
                                set(finding_schema.get("required", [])) != {r["id"] for r in packet.requirements}):
                            raise ValueError("JUDGE_PREFLIGHT_REQUIREMENT_SCHEMA_MISMATCH")
                    score_properties = schema["properties"]["scores"]["properties"]
                    if judge_revision == "v7":
                        for dimension in ("coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"):
                            expected_scores = list(range(1, 6)) if dimension in packet.applicable_dimensions else [0]
                            if score_properties[dimension]["properties"]["score"]["enum"] != expected_scores:
                                raise ValueError("JUDGE_PREFLIGHT_SCORE_APPLICABILITY_MISMATCH")
                seen += 1
    if seen != expected:
        raise ValueError(f"JUDGE_PREFLIGHT_GRID_MISMATCH:{seen}/{expected}")
    return {"status": "ready", "episodes": seen, "expected": expected,
        "run_manifest_hash": manifest["manifest_hash"], "suite": manifest["suite"],
        "variants": manifest["variants"], "judge_revision": judge_revision,
        "clean_dom": clean_dom, "max_packet_chars": max_packet_chars, "max_quote_spans": max_spans,
        "api_calls": 0}


def load_judgement(path, scope):
    original = path.with_name(f"judge_{scope}.json")
    recovery = path.with_name(f"judge_{scope}_recovery.json")
    if not original.exists():
        if recovery.exists():
            raise ValueError("Recovery has no original measurement")
        return None
    prior = json.loads(original.read_text(encoding="utf-8"))
    if not recovery.exists():
        return prior
    current = json.loads(recovery.read_text(encoding="utf-8"))
    # A persisted recovery is safe to read even when the caller is not asking to
    # create another one; the original failure and its checksum remain intact.
    recoverable = prior.get("code") in RECOVERABLE or prior.get("code") == "JUDGE_OUTPUT_INVALID"
    if (prior.get("status") != "judge_error" or not recoverable or current.get("status") not in {"ok", "judge_error"} or
        current.get("recovery_of_sha256") != hashlib.sha256(original.read_bytes()).hexdigest() or
        any(current.get(k) != prior.get(k) for k in ("protocol","episode_hash","packet_hash"))):
        raise ValueError("Recovery cannot replace a valid judgement, quality failure, or different measurement")
    is_infrastructure = prior.get("code") in RECOVERABLE
    succeeded = current['status'] == 'ok'
    return {**current,"attempts":[*prior.get("attempts",[]),*current.get("attempts",[])],
        "recovered_measurement":succeeded,"recovered_infrastructure":is_infrastructure and succeeded,
        'recovery_attempted':True, 'recovery_failed':not succeeded,
        "recovery_kind":"infrastructure" if is_infrastructure else "invalid_output"}


async def judge_run(root, browser=False, limit=None, concurrency=None, clean_dom=False, judge_revision="v3", recover_infrastructure=False, case_ids=None, recover_invalid_output=False):
    root, manifest, scenarios = load_run(root)
    settings = JudgeSettings.from_env()
    if clean_dom and judge_revision in {"v8", "v9", "v10", "v11", "v12"} and (settings.thinking or settings.format_retries != 2):
        raise ValueError(f"Judge {judge_revision} requires BANK_JUDGE_THINKING=false and BANK_JUDGE_FORMAT_RETRIES=2")
    effective_settings = replace(settings, concurrency=concurrency) if concurrency is not None else settings
    if not same_judge_scoring_configuration(settings.public(), effective_settings.public()):
        raise ValueError("Scheduling override cannot change judge scoring parameters")
    judge = ServiceJudge(effective_settings)
    judge.revision = judge_revision if clean_dom else None
    jobs = []
    scope = ("dom_"+judge_revision) if clean_dom else "dom_v2" if browser else "api_v2"
    protocol = {"judge": settings.public(), "prompt_hash": content_hash(system_prompt(revision=judge_revision if clean_dom else None)), "scope": scope,
                "run_manifest_hash": manifest["manifest_hash"]}
    if clean_dom and judge_revision in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
        protocol.update(output_mode="strict_function_call", tool_schema_version=f"bankflow-{judge_revision}-dynamic-tool-v1",
                        retry_policy={"format": settings.format_retries, "timeout": 1, "transient": 2, "backoff": "bounded_exponential"})
    revision_path = root/f"judge_protocol_{scope}.json"
    if revision_path.exists():
        if json.loads(revision_path.read_text(encoding="utf-8")) != protocol:
            raise ValueError("Judge configuration or prompt changed; preserve the old run and create a new one")
    else:
        write_json(revision_path, protocol)
    execution_id = uuid4().hex
    write_json(root/"judge_executions"/(execution_id+".json"), {
        "created_at": datetime.now(timezone.utc).isoformat(), "run_manifest_hash": manifest["manifest_hash"],
        "reference_protocol": protocol, "effective_settings": effective_settings.public(),
        "request_body_parameters_unchanged": True, "limit": limit, "recover_infrastructure":recover_infrastructure,
        "recover_invalid_output":recover_invalid_output,
        "output_normalizer_version":"strict-v8-json-field-normalization-v2" if judge_revision == "v8" else None})
    for scenario in scenarios:
        if case_ids and scenario.execution.id not in set(case_ids):
            continue
        for trial in range(1, manifest["trials"]+1):
            for variant in manifest["variants"]:
                path = episode_path(root, scenario, trial, variant)
                if path.exists():
                    jobs.append((scenario, path))
    if len(manifest["variants"]) > 1:
        random.Random(20261017).shuffle(jobs)
    if case_ids and not jobs:
        raise ValueError("No frozen episodes match --cases")
    if limit is not None:
        jobs = jobs[:limit]

    async def one(scenario, path):
        result = json.loads(path.read_text(encoding="utf-8"))
        validate_episode(result, path, root, manifest)
        target = path.with_name(f"judge_{scope}.json")
        try:
            packet = build(scenario, result, path, browser, clean_dom, judge_revision)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            if target.exists():
                raise ValueError("Previously judged packet no longer reconstructs") from exc
            write_json(path.with_name(f"packet_error_{scope}.json"), {"status": "packet_error", "code": type(exc).__name__, "protocol": protocol,
                "episode_hash": hashlib.sha256(path.read_bytes()).hexdigest()})
            return
        packet_data = packet.model_dump(mode="json")
        previous_hash = None
        if target.exists():
            old = load_judgement(path,scope)
            if old.get("protocol") != protocol or old.get("packet_hash") != content_hash(packet_data) or old.get("episode_hash") != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError("Frozen judgement differs; it cannot be overwritten or silently reused")
            if old["status"] == "ok":
                Verdict.model_validate(old["verdict"]).validate_against(packet,judge_revision)
                return
            if not measurement_recovery_allowed(old,recover_infrastructure,recover_invalid_output):
                return
            previous_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            target = path.with_name(f"judge_{scope}_recovery.json")
        write_json(path.with_name(f"packet_{scope}.json"), packet_data)
        try:
            verdict = await judge.grade(packet)
        except JudgeError as exc:
            verdict = {"status": "judge_error", "code": exc.code, "attempts": exc.attempts,
                       "packet_hash": content_hash(packet_data)}
        verdict.update(protocol=protocol, episode_hash=hashlib.sha256(path.read_bytes()).hexdigest())
        verdict.update(execution_id=execution_id, effective_judge_settings=effective_settings.public())
        if previous_hash:
            verdict["recovery_of_sha256"] = previous_hash
            if recover_invalid_output:
                verdict["recovery_normalizer_version"] = "strict-v8-json-field-normalization-v2"
        write_json(target, verdict)
        print(json.dumps({"case": scenario.execution.id, "variant": result["variant"],
                          "trial": result["trial"], "judge_status": verdict["status"]}), flush=True)

    await asyncio.gather(*(one(s, path) for s, path in jobs))


def report(root, browser=False, clean_dom=False, judge_revision="v3", _variant=None):
    root, manifest, scenarios = load_run(root)
    if len(manifest["variants"]) > 1 and _variant is None:
        from .comparison import compare, markdown
        reports = {v: report(root, browser, clean_dom, judge_revision, v) for v in manifest["variants"]}
        result = {"suite": manifest["suite"], "variants": reports,
                  "comparison": compare(root, manifest, scenarios, reports), "diagnostic_only": True}
        scope = next(iter(reports.values()))["scope"]
        write_json(root/f"report_{scope}.json", result)
        (root/f"comparison_{scope}.md").write_text(markdown(result), encoding="utf-8")
        return result
    variant = _variant or manifest["variants"][0]
    if variant not in manifest["variants"]:
        raise ValueError("Unknown report variant")
    scope, rows = (("dom_"+judge_revision) if clean_dom else "dom_v2" if browser else "api_v2"), []
    measured_episodes, judgements = [], []
    for scenario in scenarios:
        for trial in range(1, manifest["trials"]+1):
            path = episode_path(root, scenario, trial, variant)
            if not path.exists():
                continue
            result = json.loads(path.read_text(encoding="utf-8"))
            validate_episode(result, path, root, manifest)
            measured_episodes.append(result)
            judgement = load_judgement(path,scope)
            if judgement:
                judgements.append(judgement)
            try:
                packet = build(scenario, result, path, browser, clean_dom, judge_revision)
                candidate = packet.candidate_output
            except (ValueError, FileNotFoundError, KeyError):
                # Missing UI capture is a measurement gap, not proof of no answer.
                if browser:
                    row = assess(scenario, result, candidate="UI measurement unavailable")
                    row.update(response_present=None, judge_status="packet_error")
                    if row["status"] != "failed":
                        row["status"] = "unresolved"
                    row["failure_modes"].append("missing_ui_capture")
                    rows.append(row)
                    continue
                raise
            if judgement:
                protocol = json.loads((root/f"judge_protocol_{scope}.json").read_text(encoding="utf-8"))
                if not same_judge_scoring_configuration(protocol["judge"], judgement.get("effective_judge_settings", protocol["judge"])):
                    raise ValueError("Actual judge configuration differs from scoring protocol")
                if judgement.get("protocol") != protocol or judgement.get("episode_hash") != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise ValueError("Judgement no longer matches the frozen experiment")
                if judgement.get("packet_hash") != content_hash(packet.model_dump(mode="json")):
                    raise ValueError("Judgement packet changed")
                if judgement["status"] == "ok":
                    if judgement.get("prompt_hash") != content_hash(system_prompt(packet, revision=judge_revision if clean_dom else None)):
                        raise ValueError("Judgement prompt does not match this packet's frozen protocol")
                    if clean_dom and judge_revision in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"} and judgement.get("tool_schema_hash") != content_hash(strict_verdict_tool(packet, judge_revision)):
                        raise ValueError("Judgement tool schema does not match this packet's frozen protocol")
                    Verdict.model_validate(judgement["verdict"]).validate_against(packet,judge_revision)
            rows.append(assess(scenario, result, judgement, candidate))
    result = summarize(rows, len(scenarios)*manifest["trials"])
    result.update(suite=manifest["suite"], variant=variant, mode=manifest["mode"], scope=scope, trials=manifest["trials"],
                  human_labels=0, kappa=None, diagnostic_only=True, rows=rows)
    result["efficiency"] = efficiency(measured_episodes, judgements)
    result["judge_scheduling_profiles"] = [
        {"concurrency": concurrency, "graded_episodes": sum(
            j.get("effective_judge_settings", j.get("protocol", {}).get("judge", {})).get("concurrency") == concurrency
            for j in judgements)}
        for concurrency in sorted({j.get("effective_judge_settings", j.get("protocol", {}).get("judge", {})).get("concurrency", 2)
                                  for j in judgements})]
    result["fsm_coverage"] = state_coverage(measured_episodes)
    result["user_goal_fulfillment"] = {"fully_fulfilled": sum(r["goal_fulfilled"] for r in rows),
        "expected_episodes": len(scenarios)*manifest["trials"],
        "note": "Correct clarification, safe decline, constraints and capability gaps are not recorded as full goal fulfillment"}
    # The expected grid supplies group denominators, even in a partial run.
    for field in ("domain", "difficulty", "completion_target"):
        for value in {getattr(s, field) for s in scenarios}:
            total = sum(getattr(s, field) == value for s in scenarios)*manifest["trials"]
            group = result["groups"][field].setdefault(value, {"observed": 0, "passed": 0, "failed": 0})
            group.update(expected=total, pending_or_unresolved=total-group["passed"]-group["failed"],
                pass_rate_lower_bound=group["passed"]/total,
                pass_rate_upper_bound=(total-group["failed"])/total)
    result["all_trials"] = {"eligible_tasks": len(scenarios), "all_passed": sum(
        len(items := [r for r in rows if r["case_id"] == s.execution.id]) == manifest["trials"] and
        all(r["status"] == "passed" for r in items) for s in scenarios), "k": manifest["trials"]}
    expected_business = sum(s.completion_target == "execute" for s in scenarios)*manifest["trials"]
    result["business_completion"].update(expected=expected_business,
        pending_or_unobserved=expected_business-sum(r["completion_target"] == "execute" and r["status"] in {"passed", "failed"} for r in rows))
    # Successful handling of an expired assessment is not completed execution.
    # Keep transactions that can actually be fulfilled in a separate denominator.
    executable_ids = {s.execution.id for s in scenarios
                      if s.completion_target == "execute" and s.resolution == "fulfilled"}
    executable_rows = [r for r in rows if r["case_id"] in executable_ids]
    executable_expected = len(executable_ids)*manifest["trials"]
    executable_passed = sum(r["goal_fulfilled"] for r in executable_rows)
    executable_failed = sum(r["status"] == "failed" for r in executable_rows)
    executable_unknown = executable_expected-executable_passed-executable_failed
    result["fulfillable_execution_closure"] = {
        "eligible_cases": sorted(executable_ids), "expected": executable_expected,
        "passed": executable_passed, "failed": executable_failed, "pending_or_unresolved": executable_unknown,
        "pass_rate_lower_bound": executable_passed/executable_expected if executable_expected else None,
        "pass_rate_upper_bound": (executable_passed+executable_unknown)/executable_expected if executable_expected else None,
    }
    result["domain_macro_resolution"] = None
    result["family_cluster_ci95"] = None
    complete = len(rows) == len(scenarios)*manifest["trials"] and all(r["status"] in {"passed", "failed"} for r in rows)
    if complete:
        from statistics import mean
        import numpy as np
        result["domain_macro_resolution"] = mean(g["passed"]/g["expected"] for g in result["groups"]["domain"].values())
        # Average repeats per task first; resample whole scenario families.
        tasks = {s.execution.id: mean(r["status"] == "passed" for r in rows if r["case_id"] == s.execution.id) for s in scenarios}
        families = sorted({s.family for s in scenarios})
        clusters = [[tasks[s.execution.id] for s in scenarios if s.family == family] for family in families]
        rng = np.random.default_rng(20261016)
        samples = []
        for selection in rng.integers(0, len(clusters), size=(2000, len(clusters))):
            members = [v for index in selection for v in clusters[index]]
            samples.append(mean(members))
        lower, upper = np.quantile(samples, [0.025, 0.975])
        result["family_cluster_ci95"] = {"interval": [float(lower), float(upper)], "families": len(families),
            "resamples": 2000, "seed": 20261016, "repeats_averaged_before_resampling": True,
            "diagnostic_not_population_inference": True, "degenerate": bool(lower == upper)}
        if lower == upper:
            from evaluation.statistics import wilson_interval
            # Explicit family-level supplementary bound; not a claim that the
            # repeated episodes are independent binomial observations.
            successes = sum(all(v == 1 for v in cluster) for cluster in clusters)
            result["family_cluster_ci95"]["supplementary_family_all_success_wilson"] = wilson_interval(successes, len(clusters))
    suffix = "_"+variant if len(manifest["variants"]) > 1 else ""
    write_json(root/f"report_{scope}{suffix}.json", result)
    return result
