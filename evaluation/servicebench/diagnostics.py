"""Observed efficiency and FSM coverage, without treating missing data as zero."""
from collections import Counter
from numbers import Real

from bank_agent.orchestration import NodeFSM, PHASE_EDGES, Phase


def distribution(values):
    values = sorted(float(v) for v in values if isinstance(v, Real) and not isinstance(v, bool))
    def quantile(p):
        if not values:
            return None
        at = (len(values)-1)*p
        low, high = int(at), min(int(at)+1, len(values)-1)
        return round(values[low]+(values[high]-values[low])*(at-low), 3)
    return {"n": len(values), "p50": quantile(.5), "p95": quantile(.95), "max": max(values) if values else None}


def efficiency(episodes, judgements):
    def tokens(usages):
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            present = [u[key] for u in usages if isinstance(u, dict) and type(u.get(key)) is int]
            result[key] = {"known_sum": sum(present), "missing_records": len(usages)-len(present),
                           "complete_total": sum(present) if len(present) == len(usages) else None}
        return result
    attempts = [a for j in judgements for a in j.get("attempts", [])]
    model_calls = [e.get("model_call_count") for e in episodes]
    return {"episode_wall_ms": distribution([e.get("sut_wall_ms") for e in episodes]),
        "api_service_ms": distribution([e.get("sut_service_ms") for e in episodes]),
        "external_model_calls_known": sum(e.get("external_model_call_count", 0) or 0 for e in episodes),
        "model_call_count_unknown_episodes": sum(c is None for c in model_calls),
        "episodes_using_model": sum(bool(e.get("llm_exercised")) for e in episodes),
        "memory_backends": dict(Counter(e.get("memory_backend", "unknown") for e in episodes)),
        "sut_tokens": tokens([e.get("usage") for e in episodes]),
        "judge_attempts": len(attempts),
        "judge_attempt_ms": distribution([a.get("duration_ms") for a in attempts]),
        "judge_tokens": tokens([a.get("usage") for a in attempts]),
        "no_price_or_invoice_estimate": True}


def state_coverage(episodes):
    possible = {(left.value, right.value) for left, values in PHASE_EDGES.items() for right in values}
    phases, observed_edges, node_states, violations = set(), set(), set(), []
    event_count = 0
    for episode in episodes:
        # Exclude setup plans from coverage. Measured snapshots carry real plan
        # IDs; a private checkpoint contains nodes but is not a plan snapshot.
        plans = set()
        for turn in episode.get("responses", []):
            response = turn.get("response", {})
            if response.get("id") and "nodes" in response:
                plans.add(response["id"])
                node_states.update(n["status"] for n in response["nodes"])
        for event in episode.get("state", {}).get("audit", []):
            if event["kind"] != "WORKFLOW_TRANSITION" or event.get("plan_id") not in plans:
                continue
            details = event["details"]
            edge = (details.get("from"), details.get("to"))
            phases.update(edge)
            observed_edges.add(edge)
            event_count += 1
            if edge not in possible:
                violations.append({"case_id": episode["case_id"], "edge": list(edge), "audit_seq": event.get("seq")})
    all_nodes = NodeFSM.ACTIVE | NodeFSM.TERMINAL
    return {"workflow_states": {"observed": sorted(phases), "possible": len(Phase),
                "coverage": len(phases & {p.value for p in Phase})/len(Phase),
                "uncovered": sorted({p.value for p in Phase}-phases)},
        "workflow_transitions": {"observed": len(observed_edges & possible), "possible": len(possible),
                "coverage": len(observed_edges & possible)/len(possible), "event_count": event_count,
                "uncovered": [list(edge) for edge in sorted(possible-observed_edges)], "illegal_events": violations},
        "node_snapshot_states": {"observed": sorted(node_states), "possible": len(all_nodes),
                "coverage": len(node_states & all_nodes)/len(all_nodes), "uncovered": sorted(all_nodes-node_states),
                "snapshot_lower_bound_not_complete_transition_coverage": True},
        "not_a_correctness_or_real_world_failure_probability": True}
