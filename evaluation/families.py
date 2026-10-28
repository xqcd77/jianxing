"""Scenario families prevent paraphrases and repeats being treated as new tasks."""
from collections import defaultdict
import random
import statistics


def family_id(case):
    tags = case.get('tags', [])
    explicit = next((s[7:] for s in tags if s.startswith('family:')), None)
    if explicit and explicit.startswith('indirect_injection'):
        return explicit
    operations = {s.get('operation', '') for s in case.get('allowed_writes', [])}
    if len({op.split('.')[0] for op in operations if op}) > 1:
        return 'combined_allocation'
    if any(op.startswith('aa.') for op in operations):
        return 'aa_payment'
    if 'funds.transfer' in operations:
        return 'transfer_authorization'
    return case.get('category', 'unknown') if case.get('category') not in {'safety', 'reliability', 'context'} else explicit or case.get('category')


def family_bootstrap(left, right, families, iterations=5000, seed=20261015):
    if len(left) != len(right) or len(left) != len(families):
        raise ValueError('Family bootstrap inputs must be paired')
    grouped = defaultdict(list)
    for a, b, family in zip(left, right, families):
        if len(a) != len(b):
            raise ValueError('Repeat counts must match')
        grouped[family].append(statistics.mean(b) - statistics.mean(a))
    clusters = list(grouped.values())
    if not clusters:
        return {'families': 0, 'ci95': None, 'difference': None}
    generator = random.Random(seed)
    draws = sorted(statistics.mean([value for group in generator.choices(clusters, k=len(clusters)) for value in group]) for _ in range(iterations))
    return {'families': len(clusters), 'tasks': len(left), 'resampling_unit': 'scenario_family',
        'difference': statistics.mean(value for cluster in clusters for value in cluster),
        'ci95': [draws[int((iterations - 1) * .025)], draws[int((iterations - 1) * .975)]],
        'inference_ready': len(clusters) >= 2}


def holm_adjust(p_values):
    adjusted, maximum = {}, 0.0
    for index, (key, value) in enumerate(sorted(p_values.items(), key=lambda item: item[1])):
        maximum = max(maximum, min(1.0, value * (len(p_values) - index)))
        adjusted[key] = maximum
    return adjusted


def proposed_split(cases, seed=20261015, reserve_fraction=.2):
    groups = defaultdict(list)
    for case in cases:
        groups[family_id(case)].append(case['id'])
    families = sorted(groups)
    random.Random(seed).shuffle(families)
    reserved = set(families[:max(1, round(len(families) * reserve_fraction))])
    return {'split_unit': 'scenario_family', 'seed': seed, 'status': 'planning_only_not_a_blind_test_set',
        'development_ids': [key for family, keys in groups.items() if family not in reserved for key in keys],
        'reserved_ids': [key for family, keys in groups.items() if family in reserved for key in keys],
        'families': dict(groups), 'warning': 'The current tasks were used in development; this split does not make them unseen. Freeze independently authored new families for a formal test.'}
