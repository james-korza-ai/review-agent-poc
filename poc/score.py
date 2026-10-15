"""Score recorded results only. Never call a model or change engine verdicts."""
import argparse
import json
from pathlib import Path
import statistics

from . import model_adapter

RULE = 'error-handling-go/continue-after-error'
CASES = ('bug_01', 'bug_02', 'ok_03', 'ok_04', 'ok_05', 'ok_06')


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def score(reports, answer_key, expected_runs=18):
    if expected_runs != 18:
        raise ValueError('This POC plans exactly six cases and three trials each')
    if not isinstance(answer_key, list):
        raise ValueError('Answer key must be a list')
    expected = set()
    for row in answer_key:
        if not isinstance(row, dict) or set(row) != {'rule_id', 'path', 'line'} or row['rule_id'] != RULE or row['path'] not in [c + '.go' for c in CASES] or type(row['line']) is not int or row['line'] < 1:
            raise ValueError('Invalid answer key')
        key = (row['rule_id'], row['path'], row['line'])
        if key in expected:
            raise ValueError('Duplicate answer key')
        expected.add(key)
    rows = []; seen = set(); models = set(); durations = []; costs = []; tokens_in = []; tokens_out = []
    totals = {'hits': 0, 'misses': 0, 'false_flags': 0}
    unavailable_cost = 0; receipt_errors = 0
    for report in reports:
        case = report.get('case')
        trial = report.get('run')
        if type(trial) is int:
            trial = f'run-{trial}'
        if case not in CASES or trial not in ('run-1', 'run-2', 'run-3'):
            raise ValueError('Unknown case or trial')
        trial_id = f'{case}/{trial}'
        if trial_id in seen:
            raise ValueError('Duplicate trial ID')
        seen.add(trial_id)
        provenance = report.get('provenance', {})
        if not isinstance(provenance, dict):
            provenance = {}
        if isinstance(provenance.get('model'), str) and provenance['model'] != 'not-called':
            models.add(provenance['model'])
        if len(models) > 1:
            raise ValueError('A scorecard must contain exactly one actual model')
        receipts_ok = True
        try:
            # The frozen result stores numeric receipts; the cost basis belongs
            # in the run sidecar and is required by the recording CLI below.
            model_adapter.validate_receipt(provenance, require_basis=False)
        except model_adapter.OperationalError:
            receipts_ok = False
            receipt_errors += 1
        cost = provenance.get('cost_usd')
        if model_adapter.nonnegative_number(cost):
            costs.append(cost)
        else:
            unavailable_cost += 1
        duration = provenance.get('duration_ms')
        if model_adapter.nonnegative_number(duration):
            durations.append(duration)
        for key, target in [('in', tokens_in), ('out', tokens_out)]:
            token_receipt = provenance.get('tokens')
            value = token_receipt.get(key) if isinstance(token_receipt, dict) else None
            if type(value) is int and value >= 0:
                target.append(value)
        statuses = report.get('skills_run')
        complete = (not report.get('_quality_exclusion') and receipts_ok
                    and report.get('verdict', {}).get('status') == 'pass'
                    and isinstance(statuses, list) and len(statuses) == 1
                    and statuses[0].get('status') == 'ok'
                    and isinstance(report.get('findings'), list))
        row = {'case': case, 'run': trial, 'completed': complete,
               'hits': None, 'misses': None, 'false_flags': None, 'precision': None, 'recall': None,
               'provenance': provenance}
        if report.get('_quality_exclusion'):
            row['quality_exclusion'] = report['_quality_exclusion']
        if complete:
            actual = set()
            for finding in report['findings']:
                try:
                    key = (finding['rule_id'], finding['location']['path'], finding['location']['start_line'])
                    if not isinstance(key[0], str) or not isinstance(key[1], str) or type(key[2]) is not int or key[2] < 1:
                        raise ValueError('Invalid finding key')
                    actual.add(key)
                except (TypeError, KeyError):
                    raise ValueError('Malformed finding in recorded result') from None
            wanted = {item for item in expected if item[1] == case + '.go'}
            hits, misses, false_flags = len(wanted & actual), len(wanted - actual), len(actual - wanted)
            row.update(hits=hits, misses=misses, false_flags=false_flags,
                       precision=_ratio(hits, hits + false_flags), recall=_ratio(hits, hits + misses))
            for key in totals:
                totals[key] += row[key]
        rows.append(row)
    hits, misses, false_flags = (totals[k] for k in ('hits', 'misses', 'false_flags'))
    totals.update(precision=_ratio(hits, hits + false_flags), recall=_ratio(hits, hits + misses))
    planned_ids = [f'{case}/run-{n}' for case in CASES for n in range(1, 4)]
    completed = sum(row['completed'] for row in rows)
    spread = {case: {'completed': sum(r['completed'] for r in rows if r['case'] == case),
                     'observed': sum(r['case'] == case for r in rows),
                     'hits': [r['hits'] for r in rows if r['case'] == case],
                     'false_flags': [r['false_flags'] for r in rows if r['case'] == case],
                     'recall': [r['recall'] for r in rows if r['case'] == case]} for case in CASES}
    return {'model': next(iter(models), None), 'pooled': totals, 'per_run': rows, 'per_case_spread': spread,
            'coverage': {'planned': 18, 'observed': len(rows), 'completed': completed, 'unfinished': 18 - completed,
                         'missing': 18 - len(rows), 'missing_trial_ids': [key for key in planned_ids if key not in seen],
                         'incomplete_trial_ids': [f"{r['case']}/{r['run']}" for r in rows if not r['completed']]},
            'observed': {'cost_usd': sum(costs) if costs and unavailable_cost == 0 else None,
                         'unavailable_cost_receipts': unavailable_cost, 'invalid_receipts': receipt_errors,
                         'duration_ms': sum(durations) if len(durations) == len(rows) and rows else None,
                         'median_duration_ms': statistics.median(durations) if durations else None,
                         'worst_duration_ms': max(durations) if durations else None,
                         'tokens': {'in': sum(tokens_in) if len(tokens_in) == len(rows) and rows else None,
                                    'out': sum(tokens_out) if len(tokens_out) == len(rows) and rows else None}}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--answer-key', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    reports = []
    try:
        for trial_dir in sorted(args.results.glob('*/run-*')):
            if not trial_dir.is_dir():
                continue
            result_path = trial_dir / 'result.json'
            sidecar_path = trial_dir / 'result.json.diagnostics.json'
            if not sidecar_path.exists():
                sidecar_path = trial_dir / 'diagnostics.json'
            if not result_path.exists() and not sidecar_path.exists():
                continue
            report = json.loads(result_path.read_text()) if result_path.exists() else {
                'verdict': {'status': 'error'}, 'findings': [], 'skills_run': [{'status': 'error'}], 'provenance': {}}
            report.update(case=trial_dir.parent.name, run=trial_dir.name)
            # Recorded runs must preserve the raw receipts and their cost basis.
            sidecar = json.loads(sidecar_path.read_text()) if sidecar_path.exists() else {}
            receipts = sidecar.get('receipts', {})
            raw_response = receipts.get('raw_model_response')
            model = report.get('provenance', {}).get('model', '')
            simulated = (sidecar.get('mode') == 'SIMULATION' or sidecar.get('quality_evidence') is False
                         or isinstance(raw_response, dict) and raw_response.get('SIMULATION') is True
                         or isinstance(model, str) and 'SIMULAT' in model.upper())
            if simulated:
                report['_quality_exclusion'] = 'simulation'
            if simulated or not receipts.get('cost_basis') or raw_response is None:
                report['provenance'] = {**report.get('provenance', {}), 'cost_usd': None}
            reports.append(report)
        output = score(reports, json.loads(args.answer_key.read_text()))
        args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + '\n')
        args.output.chmod(0o600)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({'coverage': output['coverage'], 'pooled': output['pooled']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
