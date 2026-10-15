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
PROTOCOL = 'bounded-transient-retry-v1'
MAX_ATTEMPTS = 2
TRIAL_SECONDS = 245
BATCH_SECONDS = 900
RETRYABLE = {'model_http_429', 'model_http_503', 'model_timeout'}


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
    write_json(out/'manifest.json',{'scope':'18 logical trials, at most 36 physical attempts; synthetic code only',
        'protocol':PROTOCOL,'max_attempts_per_trial':MAX_ATTEMPTS,'batch_wall_seconds':BATCH_SECONDS,
        'trial_wall_seconds':TRIAL_SECONDS,'reserved_tokens_per_trial':TOKEN_CEILING*MAX_ATTEMPTS,
        'max_physical_attempts':18*MAX_ATTEMPTS,'reserved_tokens_per_batch':18*MAX_ATTEMPTS*TOKEN_CEILING,
        'rule_digest':load_skill(ROOT)['digest'], 'rule_version':load_skill(ROOT)['metadata']['version'],
        'suggestion_checks':'six exact-source edits must compile; proposed code is never executed',
        'cases':list(CASES),'trials_per_case':3,
        'pass_bar_status':'provisional, not team-agreed','model':os.environ.get('MODEL_ID','gemini-3.1-flash-lite'),
        'max_tokens_per_call':TOKEN_CEILING,'timeout_seconds_per_call':TIMEOUT_SECONDS,
        'cost_kind':'uncached list-price estimate, not invoice or provider hard cap'})


def _attempt(directory, number, timeout, execute, now):
    attempt=directory/'attempts'/f'attempt-{number}'
    attempt.mkdir(parents=True,exist_ok=False)
    request=json.loads((directory/'request.json').read_text())
    request['budget']={**request['budget'],'timeout_seconds':timeout,'max_tokens':TOKEN_CEILING}
    write_json(attempt/'request.json',request)
    write_json(attempt/'started.json',{'protocol':PROTOCOL,'attempt':number,
        'timeout_seconds':timeout,'reserved_tokens':TOKEN_CEILING})
    started=now()
    code=execute(attempt/'request.json',attempt/'result.json',ROOT,allow_live=True)
    diagnostic=json.loads((attempt/'result.json.diagnostics.json').read_text())
    result=json.loads((attempt/'result.json').read_text()) if code==0 else {}
    provenance=result.get('provenance',{})
    outcome={'protocol':PROTOCOL,'attempt':number,'status':'completed' if code==0 else 'failed',
        'error_code':diagnostic.get('error_code') if code else None,'duration_ms':(now()-started)*1000,
        'timeout_seconds':timeout,'reserved_tokens':TOKEN_CEILING,
        'tokens':provenance.get('tokens'),'cost_usd':provenance.get('cost_usd')}
    write_json(attempt/'outcome.json',outcome)
    return code,diagnostic,result


def run_trials(out, *, execute=run_request, now=time.monotonic, sleep=time.sleep):
    out=checked_path(out)
    batch_deadline=now()+BATCH_SECONDS
    next_start=now()
    attempts=0
    for case in CASES:
        for trial in range(1,4):
            directory=out/'results'/case/f'run-{trial}'
            trial_deadline=min(now()+TRIAL_SECONDS,batch_deadline)
            diagnostic=None
            for number in range(1,MAX_ATTEMPTS+1):
                wait=max(0,next_start-now())
                timeout=int(min(TIMEOUT_SECONDS,trial_deadline-now()-wait,batch_deadline-now()-wait))
                if timeout<1:
                    if diagnostic is not None:
                        write_json(directory/'result.json.diagnostics.json',diagnostic)
                    print('Evaluation wall budget exhausted; remaining trials are unfinished.',flush=True)
                    return 2
                if wait:sleep(wait)
                # A suspended/stalled scheduler may return late from sleep.
                # Recheck the monotonic deadlines before starting another call.
                timeout=int(min(TIMEOUT_SECONDS,trial_deadline-now(),batch_deadline-now()))
                if timeout<1:
                    if diagnostic is not None:
                        write_json(directory/'result.json.diagnostics.json',diagnostic)
                    print('Evaluation wall budget exhausted after wait; remaining trials are unfinished.',flush=True)
                    return 2
                next_start=now()+5.1
                code,diagnostic,result=_attempt(directory,number,timeout,execute,now)
                attempts+=1
                print(f'{case}/run-{trial}/attempt-{number}: '+('completed' if code==0 else 'unfinished'),flush=True)
                if code==0:
                    write_json(directory/'result.json',result)
                    write_json(directory/'result.json.diagnostics.json',diagnostic)
                    break
                error=diagnostic.get('error_code','review_failed')
                if number<MAX_ATTEMPTS and error in RETRYABLE:
                    continue
                write_json(directory/'result.json.diagnostics.json',diagnostic)
                if error.startswith('model_http_') or error in ('model_credential_missing','model_transport_failed','model_timeout'):
                    print(f'Stopped after {attempts} physical attempts: {error}; remaining trials are unfinished.',flush=True)
                    return 2
                break
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
    physical=scorecard.get('physical_attempts',{})
    if physical.get('unknown_cost_attempts',0):criteria['all_cost_estimates_recorded']=False
    return {'status':'provisional, not team-agreed','criteria':criteria,'bug_exact_hits':bugs,
            'quality_met':all(value for name,value in criteria.items() if name!='all_cost_estimates_recorded'),
            'met':all(criteria.values())}


def summarize(out):
    out=checked_path(out)
    score_main(['--results',str(out/'results'),'--answer-key',str(ROOT/'answer-key.json'),'--output',str(out/'scorecard.json')])
    scorecard=json.loads((out/'scorecard.json').read_text())
    bar=pass_bar(scorecard)
    suggestion_path=out/'suggestions.json'
    suggestions=json.loads(suggestion_path.read_text()) if suggestion_path.is_file() else {}
    bar['suggestions_met']=(suggestions.get('met') is True and suggestions.get('valid_bug_edits')==6
        and suggestions.get('clean_trials_without_findings')==12 and suggestions.get('proposed_code_executed') is False)
    bar['overall_met']=bar['met'] and bar['suggestions_met']
    physical=scorecard.get('physical_attempts',{})
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
        f"Protocol: {PROTOCOL}. Max 2 physical attempts only for HTTP 429/503 or actual timeout; no answer/schema retries.",
        f"Bounds: {BATCH_SECONDS}s batch, {TRIAL_SECONDS}s logical trial, {TIMEOUT_SECONDS}s/16k tokens per attempt; max 36 calls and 32k reserved tokens per trial.",
        f"Quality criteria met: {bar['quality_met']}. Original complete-cost bar met: {bar['met']}.",
        f"Exact-source compile-only edits: {suggestions.get('valid_bug_edits',0)}/6. Updated full bar met: {bar['overall_met']}.",
        'Compilation is not semantic approval; all suggestions require human review. No proposed program or tests execute.',
        f"Physical attempts: {physical.get('count','unavailable')}; unknown cost receipts: {physical.get('unknown_cost_attempts','unavailable')}.",
        f"Known attempt cost estimates: {physical.get('known_cost_estimate_usd')} USD; this is not the whole-batch total when any receipt is unknown.",
        'Failed attempts with unavailable usage make aggregate tokens/cost unknown; known successful estimates are not the whole-batch total.',
        'Unfinished and rejected-output trials are not counted as quiet successes. Prior batches remain separate.']
    text='\n'.join(lines)+'\n'
    (out/'readout.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as handle:handle.write(text)
    print(text)
    return 0 if bar['overall_met'] else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('prepare','run','score'))
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args(argv)
    if args.stage=='prepare':prepare(args.out);return 0
    return run_trials(args.out) if args.stage=='run' else summarize(args.out)


if __name__=='__main__':raise SystemExit(main())
