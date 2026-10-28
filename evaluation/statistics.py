"""Paired task-level statistics; repeats are retained within their task cluster."""
import math

import numpy as np


def paired_bootstrap(a, b, iterations=10000, seed=20261014, strata=None, aggregation="micro"):
    left, right = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if (left.shape != right.shape or left.ndim not in {1, 2} or not left.shape[0]
            or not np.isfinite(left).all() or not np.isfinite(right).all() or iterations < 100):
        raise ValueError("Provide equally shaped, finite paired tasks with at least 100 resamples")
    if aggregation not in {"micro", "macro"}:
        raise ValueError("Invalid aggregation")
    # Aggregate repeats before resampling; never pretend n_tasks * repeats are independent.
    differences = right - left
    if differences.ndim == 2:
        differences = differences.mean(axis=1)
    if strata is None:
        groups = [np.arange(len(differences))]
    else:
        if len(strata) != len(differences):
            raise ValueError("Strata must match task count")
        groups = [np.array([i for i, label in enumerate(strata) if label == group]) for group in dict.fromkeys(strata)]
    rng = np.random.default_rng(seed)
    pieces = [differences[rng.choice(ids, size=(iterations, len(ids)), replace=True)].mean(axis=1) for ids in groups]
    weights = np.ones(len(groups)) if aggregation == "macro" else np.array([len(ids) for ids in groups])
    samples = np.average(np.stack(pieces), axis=0, weights=weights)
    observed = np.average([differences[ids].mean() for ids in groups], weights=weights)
    low, high = np.quantile(samples, [0.025, 0.975])
    return {"difference_b_minus_a": float(observed), "ci95": [float(low), float(high)],
            "tasks": len(differences), "repeats_per_task": left.shape[1] if left.ndim == 2 else 1,
            "iterations": iterations, "seed": seed, "aggregation": aggregation,
            "degenerate_interval": bool(low == high), "resampling_unit": "task"}


def mcnemar_exact(a, b):
    left, right = np.asarray(a), np.asarray(b)
    if (left.ndim != 1 or left.shape != right.shape or not len(left)
            or not np.isin(left, [0, 1]).all() or not np.isin(right, [0, 1]).all()):
        raise ValueError("McNemar requires one paired binary result per task")
    a_only = int(((left == 1) & (right == 0)).sum())
    b_only = int(((left == 0) & (right == 1)).sum())
    n, k = a_only + b_only, min(a_only, b_only)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"a_only_success": a_only, "b_only_success": b_only, "p_value": p, "method": "exact_two_sided"}


def paired_dz(a, b):
    left, right = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if left.ndim != 1 or left.shape != right.shape or len(left) < 2 or not np.isfinite(left + right).all():
        raise ValueError("d_z requires at least two finite paired numeric scores")
    difference = right - left
    sd = float(difference.std(ddof=1))
    return {"mean_difference": float(difference.mean()), "cohen_dz": float(difference.mean() / sd) if sd else None,
            "not_computable_reason": "zero_difference_variance" if not sd else None}


def wilson_interval(successes, total):
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("Invalid binary counts")
    z = 1.959963984540054
    p, denom = successes / total, 1 + z*z / total
    center = (p + z*z / (2*total)) / denom
    margin = z * math.sqrt(p*(1-p)/total + z*z / (4*total*total)) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]
