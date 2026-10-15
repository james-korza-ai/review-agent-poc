"""Offline bounded retry protocol: real files/scoring, synthetic external outcomes."""
import contextlib
import copy
import io
import json
from pathlib import Path
import signal
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
from poc import evaluate, model_adapter, score

class Clock:
    def __init__(self):self.value=0
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds

class RetryProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name).resolve()/'evaluation';self.out.mkdir()
        self.clock=Clock();self.calls=[];self.failures=[];self.delay=1
        for case in score.CASES:
            for n in range(1,4):
                trial=self.out/'results'/case/f'run-{n}';trial.mkdir(parents=True)
                (trial/'request.json').write_text(json.dumps({'request_id':f'{case}-{n}',
                    'budget':{'max_files':1,'max_tokens':16000,'timeout_seconds':120}}))

    def execute(self,request_path,result_path,skill_dir,**kwargs):
        request=json.loads(Path(request_path).read_text());result_path=Path(result_path)
        self.calls.append((request,result_path))
        self.clock.value+=min(self.delay,request['budget']['timeout_seconds'])
        failure=self.failures.pop(0) if self.failures else None
        result_path.parent.mkdir(parents=True,exist_ok=True)
        if failure:
            diagnostic={'status':'failed','error_code':failure}
        else:
            case=request['request_id'].rsplit('-',1)[0]
            findings=[]
            if case in ('bug_01','bug_02'):
                findings=[{'rule_id':score.RULE,'location':{'path':case+'.go','start_line':25 if case=='bug_01' else 23}}]
            result={'verdict':{'status':'pass'},'findings':findings,'skills_run':[{'status':'ok'}],
                    'provenance':{'model':'gemini/test-retry-model','duration_ms':1000,'tokens':{'in':100,'out':20},'cost_usd':.001}}
            result_path.write_text(json.dumps(result))
            diagnostic={'status':'completed','dropped':0,'receipts':{'cost_basis':'synthetic estimate','raw_model_response':{'synthetic-test-response':True}}}
        Path(str(result_path)+'.diagnostics.json').write_text(json.dumps(diagnostic))
        return 2 if failure else 0

    def run_trials(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return evaluate.run_trials(self.out,execute=self.execute,now=self.clock.now,sleep=self.clock.sleep)

    def scored(self):
        key=self.out/'key.json';key.write_text(json.dumps([{'rule_id':score.RULE,'path':'bug_01.go','line':25},{'rule_id':score.RULE,'path':'bug_02.go','line':23}]))
        with contextlib.redirect_stdout(io.StringIO()):
            score.main(['--results',str(self.out/'results'),'--answer-key',str(key),'--output',str(self.out/'scorecard.json')])
        return json.loads((self.out/'scorecard.json').read_text())

    def test_one_transient_retry_preserves_both_attempts_and_unknown_total_cost(self):
        self.failures=['model_http_503',None]
        self.assertEqual(self.run_trials(),0)
        self.assertEqual(len(self.calls),19)
        trial=self.out/'results'/'bug_01'/'run-1'
        attempts=list((trial/'attempts').glob('attempt-*/outcome.json'))
        self.assertEqual(len(attempts),2)
        first=json.loads((trial/'attempts'/'attempt-1'/'outcome.json').read_text())
        self.assertEqual(first['error_code'],'model_http_503')
        self.assertIsNone(first['cost_usd']);self.assertIsNone(first['tokens'])
        self.assertTrue((trial/'attempts'/'attempt-1'/'result.json.diagnostics.json').exists())
        self.assertTrue((trial/'attempts'/'attempt-2'/'result.json').exists())
        scored=self.scored();bar=evaluate.pass_bar(scored)
        self.assertEqual(scored['coverage']['completed'],18)
        self.assertEqual(scored['physical_attempts']['count'],19)
        self.assertEqual(scored['physical_attempts']['unknown_cost_attempts'],1)
        self.assertIsNone(scored['observed']['cost_usd'])
        self.assertIsNone(scored['observed']['tokens']['in'])
        self.assertTrue(bar['quality_met'])
        self.assertFalse(bar['criteria']['all_cost_estimates_recorded'])
        self.assertFalse(bar['met'])

    def test_only_429_503_and_real_timeout_retry_and_never_more_than_twice(self):
        for transient in ('model_http_429','model_http_503','model_timeout'):
            with self.subTest(code=transient):
                # Each scenario gets fresh evidence; no overwrite/replay.
                previous=self.out;self.out=previous/transient;self.out.mkdir()
                for case in score.CASES:
                    for n in range(1,4):
                        trial=self.out/'results'/case/f'run-{n}';trial.mkdir(parents=True)
                        (trial/'request.json').write_bytes((previous/'results'/case/f'run-{n}'/'request.json').read_bytes())
                self.calls=[];self.failures=[transient,transient]
                self.assertEqual(self.run_trials(),2)
                self.assertEqual(len(self.calls),2)
                self.assertEqual(self.scored()['coverage']['completed'],0)
                self.out=previous

    def test_valid_answer_schema_failure_other_http_and_transport_never_retry(self):
        for failure in (None,'review_failed','model_decode_invalid','model_http_400','model_http_404','model_transport_failed'):
            with self.subTest(code=failure):
                previous=self.out;self.out=previous/str(failure);self.out.mkdir()
                for case in score.CASES:
                    for n in range(1,4):
                        trial=self.out/'results'/case/f'run-{n}';trial.mkdir(parents=True)
                        (trial/'request.json').write_bytes((previous/'results'/case/f'run-{n}'/'request.json').read_bytes())
                self.calls=[];self.failures=[failure]
                self.run_trials()
                first=[path for request,path in self.calls if request['request_id']=='bug_01-1']
                self.assertEqual(len(first),1)
                self.out=previous

    def test_batch_and_trial_deadlines_clip_attempts_and_stop_new_calls(self):
        self.delay=120
        self.run_trials()
        self.assertLessEqual(self.clock.value,900)
        self.assertLess(len(self.calls),18)
        self.assertTrue(all(0<r['budget']['timeout_seconds']<=120 for r,p in self.calls))
        self.assertLessEqual(len(self.calls),36)

    def test_existing_attempt_evidence_refuses_replay(self):
        self.run_trials();before=len(self.calls)
        with self.assertRaises((ValueError,FileExistsError)):
            self.run_trials()
        self.assertEqual(len(self.calls),before)

    def test_no_retry_for_rejected_findings_and_columns_or_schema_failures(self):
        def rejected(request,result,skill,**kwargs):
            code=self.execute(request,result,skill,**kwargs)
            path=Path(str(result)+'.diagnostics.json')
            diagnostic=json.loads(path.read_text());diagnostic['dropped']=1
            path.write_text(json.dumps(diagnostic))
            return code
        with contextlib.redirect_stdout(io.StringIO()):
            evaluate.run_trials(self.out,execute=rejected,now=self.clock.now,sleep=self.clock.sleep)
        self.assertEqual(len(self.calls),18)
        self.assertEqual(self.scored()['coverage']['completed'],0)

    def test_no_unknown_usage_when_all_attempts_complete_without_retry(self):
        self.run_trials();scored=self.scored();bar=evaluate.pass_bar(scored)
        self.assertEqual(scored['physical_attempts']['count'],18)
        self.assertEqual(scored['physical_attempts']['unknown_cost_attempts'],0)
        self.assertAlmostEqual(scored['observed']['cost_usd'],.018)
        self.assertEqual(scored['observed']['tokens'],{'in':1800,'out':360})
        self.assertTrue(bar['quality_met']);self.assertTrue(bar['met'])

    def test_remaining_trial_time_clips_second_attempt(self):
        self.failures=['model_http_503','model_http_503']
        def overhead(request,result,skill,**kwargs):
            code=self.execute(request,result,skill,**kwargs)
            if len(self.calls)==1:self.clock.value=244
            return code
        with contextlib.redirect_stdout(io.StringIO()):
            evaluate.run_trials(self.out,execute=overhead,now=self.clock.now,sleep=self.clock.sleep)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][0]['budget']['timeout_seconds'],1)
        self.assertLessEqual(self.clock.value,245)

    def test_missing_physical_receipts_and_started_only_attempts_remain_unknown(self):
        self.run_trials()
        first=self.out/'results'/'bug_01'/'run-1'/'attempts'/'attempt-1'
        (first/'result.json.diagnostics.json').unlink()
        scored=self.scored()
        self.assertIsNone(scored['observed']['cost_usd'])
        self.assertEqual(scored['physical_attempts']['unknown_cost_attempts'],1)
        self.assertFalse(evaluate.pass_bar(scored)['met'])
        (first/'outcome.json').unlink()
        scored=self.scored()
        self.assertEqual(scored['physical_attempts']['count'],18)
        self.assertEqual(scored['physical_attempts']['unknown_cost_attempts'],1)
        self.assertIsNone(scored['physical_attempts']['duration_ms'])

    def test_scheduler_oversleep_cannot_start_retry_after_trial_or_batch_deadline(self):
        self.failures=['model_http_503',None]
        def oversleep(seconds):self.clock.value+=1000
        with contextlib.redirect_stdout(io.StringIO()):
            result=evaluate.run_trials(self.out,execute=self.execute,now=self.clock.now,sleep=oversleep)
        self.assertEqual(result,2)
        self.assertEqual(len(self.calls),1)
        trial=self.out/'results'/'bug_01'/'run-1'
        self.assertTrue((trial/'result.json.diagnostics.json').exists())
        self.assertFalse((trial/'attempts'/'attempt-2').exists())

class TimeoutTests(unittest.TestCase):
    def transport(self,timeout=.02):
        return model_adapter._gemini_transport('rule',{},endpoint='https://generativelanguage.googleapis.com/v1beta/models/test:generateContent',model='test',key='synthetic-key',timeout_seconds=timeout,max_tokens=1000)

    def test_real_timeout_types_are_distinct_from_timeout_looking_strings(self):
        for failure,expected in [(TimeoutError('private'),'model_timeout'),
            (urllib.error.URLError(TimeoutError('private')),'model_timeout'),
            (urllib.error.URLError('timed out private'),'model_transport_failed')]:
            with self.subTest(failure=type(failure).__name__),patch.object(model_adapter.urllib.request,'build_opener') as opener:
                opener.return_value.open.side_effect=failure
                with self.assertRaises(model_adapter.OperationalError) as raised:self.transport()
                self.assertEqual(raised.exception.code,expected)

    def test_http_wall_deadline_interrupts_read_and_restores_signal_handler(self):
        class SlowRead(io.BytesIO):
            def read(self,*args):time.sleep(.2);return b'{}'
        class Endpoint:
            def open(self,*args,**kwargs):return SlowRead()
        handler=signal.getsignal(signal.SIGALRM);before=time.monotonic()
        with patch.object(model_adapter.urllib.request,'build_opener',return_value=Endpoint()):
            with self.assertRaises(model_adapter.OperationalError) as raised:self.transport(.02)
        self.assertEqual(raised.exception.code,'model_timeout')
        self.assertLess(time.monotonic()-before,.15)
        self.assertEqual(signal.getsignal(signal.SIGALRM),handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_existing_caller_deadline_is_neither_replaced_nor_extended(self):
        previous=signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL,5)
        try:
            with patch.object(model_adapter.urllib.request,'build_opener') as opener:
                with self.assertRaises(model_adapter.OperationalError) as raised:self.transport()
                self.assertEqual(raised.exception.code,'model_configuration_invalid')
                opener.return_value.open.assert_not_called()
            remaining,_=signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining,4)
            self.assertLessEqual(remaining,5)
            self.assertEqual(signal.getsignal(signal.SIGALRM),previous)
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)

    def test_completed_late_response_is_over_budget_and_never_retryable_timeout(self):
        class Endpoint:
            def open(self,*args,**kwargs):return io.BytesIO(b'{}')
        with patch.object(model_adapter.urllib.request,'build_opener',return_value=Endpoint()),patch.object(model_adapter.time,'monotonic',side_effect=[0,2]):
            with self.assertRaises(model_adapter.OperationalError) as raised:self.transport(1)
        self.assertEqual(raised.exception.code,'model_budget_exceeded')
        self.assertNotIn(raised.exception.code,evaluate.RETRYABLE)
