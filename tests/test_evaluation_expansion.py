import json
import time
from collections import Counter
import pytest

from evaluation.dev100 import dev100
from evaluation.judges import compare
from evaluation.metrics import diagnostics, paired_security
from evaluation.review import cohen_kappa, review_agreement
from evaluation.watchdog import supervise


def test_dev100_is_executable_and_gold_has_business_diversity():
    cases = dev100()
    assert len(cases) == len({c.id for c in cases}) == 100
    assert Counter(c.group for c in cases) == {'business': 33, 'dialogue': 22, 'research': 15, 'security': 15, 'recovery': 15}
    assert len({c.balance_fen for c in cases}) > 20
    assert {c.risk_level for c in cases} == {1, 2, 3, 4, 5}
    assert any(c.risk_valid_until < '2026-10-13' for c in cases)
    assert all(c.checks and c.expected_behavior for c in cases)


def test_query_gold_rejects_plausible_wrong_candidate_sets():
    assert compare(['P_CASH', 'P_STEADY'], 'set_eq', ['P_STEADY', 'P_CASH'])
    assert not compare(['P_CASH', 'P_LOCKED'], 'set_eq', ['P_CASH', 'P_STEADY'])
    assert not compare(None, 'set_eq', [])


def test_attack_pairs_change_only_the_untrusted_text():
    from evaluation.attack_pairs import security12
    cases = security12()
    assert len(cases) == 12
    for clean, attacked in zip(cases[::2], cases[1::2]):
        assert clean.steps == attacked.steps and clean.checks == attacked.checks
        assert clean.balance_fen == attacked.balance_fen
        assert clean.patches[0]['kind'] == attacked.patches[0]['kind']
        assert clean.patches[0]['text'] != attacked.patches[0]['text']


def test_no_model_exposure_is_not_reported_as_zero_attack_success():
    rows = [{'case_id': name, 'trial': 1, 'rules': {'safe_success': True, 'policy_failures': []},
             'model_calls': [], 'state': {}} for name in ('clean', 'attack')]
    metadata = {'clean': {'tags': ['pair:p', 'condition:clean']}, 'attack': {'tags': ['pair:p', 'condition:attacked']}}
    assert paired_security(rows, metadata)['attack_success_rate'] is None
    rows[1]['model_calls'] = [{'probe_matches': {'attack': True}, 'http_status': 200}]
    assert paired_security(rows, metadata)['attack_success_rate'] == 0
    rows[1]['state']['mutating_receipt_count'] = 1
    assert paired_security(rows, metadata)['attack_success_rate'] == 1


def test_all_three_is_distinct_from_at_least_once_success():
    rows = []
    for trial, win in enumerate((True, False, True), 1):
        rows.append({'case_id': 'c', 'variant': 'C', 'trial': trial, 'category': 'funds', 'rules': {'safe_success': win},
            'responses': [], 'model_calls': []})
    result = diagnostics(rows, [], {'variants': ['C'], 'cases': ['c'], 'trials': 3})['C']['all_repeats']
    assert result['rate'] == 0 and result['all_success'] == 0


def test_hard_watchdog_terminates_a_stuck_process():
    start = time.monotonic()
    result = supervise(time.sleep, (10,), .2)
    assert result['expired'] and time.monotonic() - start < 7


def test_kappa_uses_actual_pairs_and_handles_degenerate_labels():
    assert cohen_kappa([], [])['kappa'] is None
    assert cohen_kappa([True, True], [True, True])['kappa'] is None
    assert cohen_kappa([False, True], [False, True])['kappa'] == 1
    assert cohen_kappa([1, 5], [2, 4], ordinal=True)['kappa'] > 0


def test_family_split_does_not_leak_the_same_family_between_groups():
    from evaluation.families import family_bootstrap, holm_adjust, proposed_split
    rows = [dict(id=f'case_{i}', tags=['family:' + str(i//2)]) for i in range(10)]
    split = proposed_split(rows)
    for family, ids in split['families'].items():
        assert set(ids) <= set(split['development_ids']) or set(ids) <= set(split['reserved_ids'])
    assert family_bootstrap([[0], [0], [1]], [[1], [1], [1]], ['same', 'same', 'other'])['families'] == 2
    assert holm_adjust({'a': .01, 'b': .04}) == {'a': .02, 'b': .04}


def test_report_family_counts_use_the_same_grouping_as_split_and_bootstrap():
    cases = [dict(id=f'variant_{i}', category='funds', allowed_writes=[{'operation':'funds.transfer'}],
        tags=['family:variant_' + str(i)]) for i in range(3)]
    episodes = [dict(case_id=c['id'],variant='C',category='funds',rules={'safe_success':True},responses=[],model_calls=[]) for c in cases]
    result = diagnostics(episodes,cases,{'variants':['C'],'cases':[c['id'] for c in cases],'trials':1})['C']
    assert result['scenario_family_count'] == 1
    assert result['scenario_family_sizes'] == {'transfer_authorization':3}


def test_unfilled_human_templates_do_not_produce_agreement(tmp_path):
    from evaluation.rubrics import DIMENSIONS
    paths = []
    for name in ('rater_a', 'rater_b'):
        path = tmp_path / (name + '.jsonl')
        row = {'sample_id': 'same', 'manifest_hash': 'run', 'rater': name, 'annotation_source': 'pending',
               'safe_success': None, 'ratings': dict.fromkeys(DIMENSIONS)}
        path.write_text(json.dumps(row) + '\n', encoding='utf-8')
        paths.append(path)
    result = review_agreement(paths, tmp_path / 'agreement.json')
    assert not result['human_double_labelled'] and result['outcome_agreement']['kappa'] is None
    with pytest.raises(ValueError):
        review_agreement([paths[0], paths[0]], tmp_path / 'invalid.json')


def test_trace_proves_probe_in_emitted_messages_without_storing_input():
    import httpx
    from bank_agent.agents import ModelAdapter
    from bank_agent.config import Settings
    from bank_agent.schemas import Intent
    from evaluation.trace import Trace
    model = ModelAdapter(Settings(data_dir=__import__('pathlib').Path('.data'), model_mode='llm', model_api_key='fixture'))
    model.http_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'choices': [{'finish_reason':'stop',
        'message':{'content':'{"summary":"查询","tasks":[]}'}}]}))
    trace = Trace()
    trace.probes['source_1'] = 'synthetic-untrusted-marker'
    trace.observe(model)
    asyncio = __import__('asyncio')
    asyncio.run(model.astructured('planner', {'data':'synthetic-untrusted-marker'}, Intent))
    assert trace.models[0]['probe_matches']['source_1'] and trace.models[0]['http_status'] == 200
    assert 'synthetic-untrusted-marker' not in str(trace.models)
