"""Fresh paired Lite66 execution, immutable UI replay and MiMo v12 grading."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx

from bank_agent.config import Settings
from bank_agent.memory import Embeddings
from evaluation.config import JudgeSettings
from evaluation.integrity import write_json
from evaluation.servicebench.capture import capture
from evaluation.servicebench.runner import freeze, run
from evaluation.servicebench.scoring import summarize
from evaluation.servicebench.workflow import judge_run, preflight_judge, report

WEIGHTS = {'coverage': 2, 'banking_correctness': 2, 'evidence_reasoning': 1, 'actionability': 1, 'clarity': 1}
ARMS = ('S_AGENT', 'SUPERVISOR')


def compact(rows, expected):
    result = summarize(rows, expected)
    dimensions = result['quality_means_judged_only']
    result['weighted_quality_5'] = round(sum(dimensions[k]['mean'] * w for k, w in WEIGHTS.items()) / 7, 4) \
        if set(WEIGHTS) <= dimensions.keys() else None
    result['state_checks_passed'] = sum(r['objective_checks_pass'] for r in rows)
    return result


def network_preflight(control, update):
    business, judge = Settings.from_env(), JudgeSettings.from_env()
    if not business.model_api_key or not judge.api_key:
        raise RuntimeError('BUSINESS_OR_JUDGE_KEY_NOT_CONFIGURED')
    outputs = []
    with httpx.Client(timeout=60) as client:
        for provider, url, key, model, budget in [
            ('business', business.model_base_url, business.model_api_key, business.model_name, {'max_tokens': 16}),
            ('judge', judge.base_url, judge.api_key, judge.model, {'max_completion_tokens': 16})]:
            response = client.post(url.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + key, **({'api-key': key} if provider == 'judge' else {})},
                json={'model': model, 'messages': [{'role': 'user', 'content': 'Reply only OK.'}],
                      'thinking': {'type': 'disabled'}, **budget})
            item = {'provider': provider, 'model': model, 'status_code': response.status_code}
            if response.status_code == 200:
                item['usage'] = response.json().get('usage')
            outputs.append(item)
            write_json(control / 'network_preflight.json', {'calls_outside_scored_grid': outputs})
            update('network_preflight', result=item)
            if response.status_code != 200:
                raise RuntimeError(provider.upper() + '_PREFLIGHT_HTTP_' + str(response.status_code))


def execute(root, workers, selected, mode, update, variants=ARMS):
    configured = Settings.from_env()
    memory = 'fastembed' if mode == 'llm' else 'lexical'
    if memory == 'fastembed':
        embedding = Embeddings(configured)
        embedding.encode(['隔离66题评测模型缓存校验'])
        if embedding.backend != 'fastembed':
            raise RuntimeError('SEMANTIC_EMBEDDINGS_UNAVAILABLE')
    _, scenarios, manifest = freeze(root, selected=selected, mode=mode, trials=1, memory=memory,
        workers=workers, variants=variants, suite='lite66')
    if memory == 'fastembed':
        cache = configured.data_dir / 'embedding_cache'
        immutable = [p for p in cache.rglob('*') if p.is_file() and p.suffix not in {'.incomplete', '.lock'} and '.locks' not in p.parts]
        for scenario in scenarios:
            for arm in variants:
                target = root / arm / scenario.execution.id / 'trial_1/sandbox/embedding_cache'
                for source in immutable:
                    destination = target / source.relative_to(cache)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        os.link(source.resolve(), destination)
    update('business_execution', expected=len(scenarios) * len(variants), manifest_hash=manifest['manifest_hash'])
    run(root, selected=selected, mode=mode, trials=1, memory=memory, workers=workers, variants=variants, suite='lite66')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.data/evaluation/lite66_20261027_v12')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--variants', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--phase', choices=['preflight', 'business', 'capture', 'judge', 'report', 'all'], default='all')
    parser.add_argument('--mode', choices=['demo', 'llm'], default='llm')
    parser.add_argument('--selected', nargs='*', default=[])
    parser.add_argument('--recover-measurement-errors', action='store_true')
    args = parser.parse_args()
    variants = tuple(args.variants)
    if len(set(variants)) != len(variants):
        raise ValueError('Use unique variants')
    # Explicit, same scoring protocol for both arms; never inherit stale defaults.
    os.environ.update(BANK_JUDGE_THINKING='false', BANK_JUDGE_FORMAT_RETRIES='2',
        BANK_JUDGE_MAX_COMPLETION_TOKENS='8192', BANK_JUDGE_TIMEOUT='240')
    root = Path(args.root).resolve()
    control = root.with_name(root.name + '_control')
    control.mkdir(parents=True, exist_ok=True)
    status = {'root': str(root), 'variants': variants, 'suite': 'lite66', 'judge_revision': 'v12', 'trials': 1}

    def update(phase, **extra):
        status.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
        write_json(control / 'status.json', status)
        print(json.dumps({'phase': phase, **extra}, ensure_ascii=False), flush=True)

    try:
        if args.phase in {'all', 'preflight'} and args.mode == 'llm':
            network_preflight(control, update)
        if args.phase in {'all', 'business'}:
            execute(root, args.workers, args.selected, args.mode, update, variants)
        if args.phase in {'all', 'capture'}:
            update('ui_capture')
            capture(root, clean_dom=True, revision='v3')
        if args.phase in {'all', 'judge'}:
            ready = preflight_judge(root, browser=True, clean_dom=True, judge_revision='v12')
            write_json(root / 'preflight_v12.json', ready)
            update('independent_judging', **ready)
            asyncio.run(judge_run(root, browser=True, clean_dom=True, judge_revision='v12', concurrency=2,
                recover_infrastructure=args.recover_measurement_errors, recover_invalid_output=args.recover_measurement_errors))
        if args.phase in {'all', 'judge', 'report'}:
            measured = report(root, browser=True, clean_dom=True, judge_revision='v12')
            summary = {'suite': measured['suite'], 'weights': WEIGHTS, 'run_root': str(root), 'variants': {}}
            report_arms = tuple(measured['variants']) if 'variants' in measured else (measured['variant'],)
            reports = measured['variants'] if 'variants' in measured else {report_arms[0]: measured}
            for arm in report_arms:
                rows = reports[arm]['rows']
                subsets = {'all66': (rows, 66),
                    'original58': ([r for r in rows if not r['case_id'].startswith(('memory_l', 'context_u'))], 58),
                    'memory4': ([r for r in rows if r['case_id'].startswith('memory_l')], 4),
                    'context4': ([r for r in rows if r['case_id'].startswith('context_u')], 4)}
                summary['variants'][arm] = {name: compact(items, expected) for name, (items, expected) in subsets.items()}
                summary['variants'][arm]['rows'] = rows
                summary['variants'][arm]['efficiency'] = reports[arm]['efficiency']
            write_json(root / 'score_summary_v12.json', summary)
            write_json(control / 'summary.json', summary)
            complete = all(summary['variants'][a]['all66']['judge_valid'] == 66 for a in report_arms)
            update('completed' if complete else 'completed_with_measurement_gaps',
                results={a: summary['variants'][a]['all66'] for a in report_arms})
    except BaseException as error:
        update('error', error_type=type(error).__name__, error_code=str(error)[:150])
        raise


if __name__ == '__main__':
    main()
