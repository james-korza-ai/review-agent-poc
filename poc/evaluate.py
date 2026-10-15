"""Run the fixed six-fixture, three-trial evaluation; never post PR findings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from .build_request import build_request, checked_path, write_json
from .run import run_request, load_skill
from .score import CASES, main as score_main

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 120
TOKEN_CEILING = 16000


def _git(repo, *args):
    # This preparation step has no model key and operates only on owned fixtures.
    environment = {key: os.environ[key] for key in ('PATH', 'TMPDIR', 'LANG') if key in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    process = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null',
        '-c', 'user.name=Synthetic evaluation', '-c', 'user.email=evaluation@example.invalid',
        '-C', str(repo), *args], capture_output=True, text=True, env=environment, timeout=15)
    if process.returncode:raise ValueError('Owned fixture Git preparation failed')
    return process.stdout.strip()


def prepare(out):
    out=checked_path(out)
    out.mkdir(parents=True, exist_ok=False)
    config=json.loads((ROOT/'.review-agent.json').read_text())
    config['budget']={'max_files':1,'max_tokens':TOKEN_CEILING,'timeout_seconds':TIMEOUT_SECONDS}
    for case in CASES:
        repo=out/'sources'/case
        repo.mkdir(parents=True)
        _git(repo,'init','-q')
        (repo/'.review-agent.json').write_text(json.dumps(config))
        filename=case+'.go'
        (repo/filename).write_text('package fixtures\n')
        _git(repo,'add','--','.review-agent.json',filename)
        _git(repo,'commit','-qm','test: add minimal evaluation baseline')
        base=_git(repo,'rev-parse','HEAD')
        (repo/filename).write_bytes((ROOT/'fixtures'/filename).read_bytes())
        _git(repo,'add','--',filename)
        _git(repo,'commit','-qm','test: add synthetic evaluation input')
        head=_git(repo,'rev-parse','HEAD')
        for trial in range(1,4):
            build_request(repo,base,head,out/'results'/case/f'run-{trial}'/'request.json')
    write_json(out/'manifest.json',{'scope':'18 actual model attempts; synthetic code only',
        'rule_digest':load_skill(ROOT)['digest'],'cases':list(CASES),'trials_per_case':3,
        'pass_bar_status':'provisional, not team-agreed','model':os.environ.get('MODEL_ID','gemini-3.1-flash-lite'),
        'max_tokens_per_call':TOKEN_CEILING,'timeout_seconds_per_call':TIMEOUT_SECONDS,
        'cost_kind':'uncached list-price estimate, not invoice or provider hard cap'})


def run_trials(out):
    out=checked_path(out)
    next_start=0
    attempts=0
    # Explicitly bounded spacing, no retry or substitution of failed trials.
    for case in CASES:
        for trial in range(1,4):
            directory=out/'results'/case/f'run-{trial}'
            wait=next_start-time.monotonic()
            if wait>0:time.sleep(wait)
            started=time.monotonic()
            next_start=started+5.1
            code=run_request(directory/'request.json',directory/'result.json',ROOT,allow_live=True)
            attempts+=1
            print(f'{case}/run-{trial}: '+('completed' if code==0 else 'unfinished'),flush=True)
            if code:
                diagnostic=json.loads((directory/'result.json.diagnostics.json').read_text())
                error=diagnostic.get('error_code','review_failed')
                # Do not make seventeen more calls against a denied/unavailable service.
                if error.startswith('model_http_') or error in ('model_credential_missing','model_transport_failed'):
                    print(f'Stopped after {attempts} attempts: {error}; remaining trials are unfinished.',flush=True)
                    return 2
    return 0


def pass_bar(scorecard):
    rows=scorecard['per_run']
    clean=[r for r in rows if r['case'].startswith('ok_')]
    bugs={case:sum(r['completed'] and (r.get('hits') or 0)>=1 and r.get('false_flags')==0
                   for r in rows if r['case']==case) for case in ('bug_01','bug_02')}
    criteria={
        'zero_false_flags_in_12_clean_runs':len(clean)==12 and all(r['completed'] and r.get('false_flags')==0 for r in clean),
        'each_bug_caught_on_exact_line_at_least_twice':all(count>=2 for count in bugs.values()),
        'all_18_complete_within_call_budgets':len(rows)==18 and scorecard['coverage']['completed']==18 and all(
            r['completed'] and r['provenance']['duration_ms']<=TIMEOUT_SECONDS*1000 and
            sum(r['provenance']['tokens'].values())<=TOKEN_CEILING for r in rows),
        'all_cost_estimates_recorded':len(rows)==18 and scorecard['observed']['invalid_receipts']==0 and
            scorecard['observed']['unavailable_cost_receipts']==0}
    return {'status':'provisional, not team-agreed','criteria':criteria,'bug_exact_hits':bugs,'met':all(criteria.values())}


def summarize(out):
    out=checked_path(out)
    score_main(['--results',str(out/'results'),'--answer-key',str(ROOT/'answer-key.json'),'--output',str(out/'scorecard.json')])
    scorecard=json.loads((out/'scorecard.json').read_text())
    bar=pass_bar(scorecard)
    write_json(out/'pass-bar.json',bar)
    lines=['# Six-fixture evaluation','',f"Model: {scorecard['model']}",
        f"Completed: {scorecard['coverage']['completed']}/18. Unfinished: {scorecard['coverage']['unfinished']}.",
        '', '| Case | Completed | Exact hits | False flags |','|---|---:|---:|---:|']
    for case in CASES:
        rows=[r for r in scorecard['per_run'] if r['case']==case]
        lines.append(f"| {case} | {sum(r['completed'] for r in rows)}/3 | {sum(r.get('hits') or 0 for r in rows)} | {sum(r.get('false_flags') or 0 for r in rows)} |")
    lines+=['','Provisional bar, not team-agreed:']
    for name,met in bar['criteria'].items():lines.append(f"- {'✅' if met else '❌'} {name.replace('_',' ')}")
    lines+=['',f"Recorded usage cost estimate: {scorecard['observed']['cost_usd']} USD, not a bill or spend cap.",
        'Six fixtures on one model do not establish the 0.90 graduation bar, vendor parity, or quality on another model.',
        'Unfinished and rejected-output trials are not counted as quiet successes. No trial was retried.']
    text='\n'.join(lines)+'\n'
    (out/'readout.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as handle:handle.write(text)
    print(text)
    return 0 if bar['met'] else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('prepare','run','score'))
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args(argv)
    if args.stage=='prepare':prepare(args.out);return 0
    return run_trials(args.out) if args.stage=='run' else summarize(args.out)


if __name__=='__main__':raise SystemExit(main())
