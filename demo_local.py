"""Offline plumbing demonstration. Responses are constructed, not AI reviews.

Creates a new local synthetic Git repository with no remote, then feeds two
real commit diffs through the engine using explicitly simulated responses.
Never writes results/ or calls the quality scorer. No API key is needed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from poc import build_request, check_runner, run

ROOT = Path(__file__).resolve().parent


def _git(repo, *args):
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null',
                             '-c', 'user.name=Synthetic POC',
                             '-c', 'user.email=poc@example.invalid',
                             '-C', str(repo), *args], env=environment,
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Owned demonstration Git operation failed')
    return result.stdout.strip()


def demonstrate(output):
    output = build_request.checked_path(output)
    output.mkdir(parents=True, exist_ok=False)
    repository = output / 'repository'
    repository.mkdir()
    _git(repository, 'init', '-q', '-b', 'main')
    sample = json.loads((ROOT / 'samples/request.json').read_text())
    policy = {k: sample[k] for k in ('skills', 'gate', 'budget')}
    (repository / '.review-agent.json').write_text(json.dumps(policy))
    (repository / 'review.go').write_text('package fixtures\n')
    _git(repository, 'add', '--', '.review-agent.json', 'review.go')
    _git(repository, 'commit', '-q', '-m', 'test: add synthetic baseline')
    base = _git(repository, 'rev-parse', 'HEAD')
    summary = {'mode': 'SIMULATION', 'live_model_calls': 0, 'quality_trials': 0,
               'note': 'Constructed responses test plumbing, not AI detection. No network or GitHub writes.',
               'repository': str(repository), 'cases': []}
    for name, fixture, flagged_line in [('broken', 'bug_01.go', 25),
                                         ('valid', 'ok_03.go', None)]:
        (repository / 'review.go').write_bytes((ROOT / 'fixtures' / fixture).read_bytes())
        _git(repository, 'add', '--', 'review.go')
        _git(repository, 'commit', '-q', '-m', 'test: add synthetic ' + name + ' example')
        head = _git(repository, 'rev-parse', 'HEAD')
        case_dir = output / name
        request = case_dir / 'request.json'
        build_request.build_request(repository, base, head, request)
        findings = []
        if flagged_line is not None:
            findings = [{'rule_id': build_request.RULE, 'skill': 'error-handling-go',
                         'severity': 'medium',
                         'location': {'path': 'review.go', 'start_line': flagged_line,
                                      'end_line': flagged_line, 'start_col': 0, 'end_col': 0},
                         'message': 'The value is used after the fetch failed.',
                         'suggestion': {'text': 'Return or choose a valid fallback before using it.',
                                        'replacement': ''}}]
        response = {'text': json.dumps(findings),
                    'raw_response': {'SIMULATION': True, 'findings': findings},
                    'model': 'gemini/SIMULATED-NOT-A-MODEL-RUN',
                    'duration_ms': 0, 'tokens': {'in': 0, 'out': 0}, 'cost_usd': 0,
                    'receipt_basis': 'SIMULATION: no provider call, usage or bill; not quality evidence'}
        with patch.object(check_runner.model_adapter, 'call_model', return_value=response) as simulated:
            code = run.run_request(request, case_dir / 'result.json', ROOT)
        if code or simulated.call_count != 1:
            raise RuntimeError('Simulated engine flow did not complete exactly one check')
        result = json.loads((case_dir / 'result.json').read_text())
        item = {'name': name, 'findings': len(result['findings']),
                'verdict': result['verdict']['status'], 'head_sha': head}
        summary['cases'].append(item)
        sidecar = case_dir / 'result.json.diagnostics.json'
        diagnostics = json.loads(sidecar.read_text())
        diagnostics.update(mode='SIMULATION', quality_evidence=False)
        sidecar.write_text(json.dumps(diagnostics, indent=2) + '\n')
    (output / 'SIMULATION.json').write_text(json.dumps(summary, indent=2) + '\n')
    (output / 'README.txt').write_text(
        'SIMULATION ONLY\nTwo real Git diffs; constructed model responses.\n'
        'This demonstrates validation and advisory reporting, not AI detection.\n'
        'There is no remote repository, model request, API key or quality scorecard.\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path, help='new owned directory; never overwritten')
    args = parser.parse_args()
    summary = demonstrate(args.out)
    print('SIMULATION ONLY — no model calls or quality score')
    for case in summary['cases']:
        print(f"{case['name']}: {case['findings']} finding(s); advisory verdict {case['verdict']}")
    print('Local synthetic repository: ' + summary['repository'])


if __name__ == '__main__':
    main()
