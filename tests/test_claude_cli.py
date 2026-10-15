"""Offline CLI adapter tests. Constructed replies do not count as AI trials."""
import json
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch
from poc import model_adapter

class ClaudeCliTests(unittest.TestCase):
    def test_timeout_kills_owned_child_processes_holding_pipes(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):
                model_adapter.run_claude_process([sys.executable, '-c',
                    'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(5)"]);time.sleep(5)'],
                    Path(temp).resolve(), {}, '', 0.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_decode_rejects_malformed_missing_cost_and_wrong_models(self):
        for raw in [None, [], '[]', {}, {'subtype': 'error'}]:
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                model_adapter.decode_claude_cli(raw)

    def test_decode_records_cli_estimate_and_actual_usage_not_self_reported_confidence(self):
        response={'subtype':'success','is_error':False,'result':'[]','num_turns':1,
                  'duration_ms':45,'total_cost_usd':0.012,
                  'usage':{'input_tokens':10,'cache_creation_input_tokens':20,
                           'cache_read_input_tokens':30,'output_tokens':4},
                  'modelUsage':{'claude-opus-5-5':{}}}
        out=model_adapter.decode_claude_cli(response)
        self.assertEqual(out['model'],'claude-cli/claude-opus-5-5')
        self.assertEqual(out['tokens'],{'in':60,'out':4})
        self.assertIn('estimate',out['receipt_basis'])
        self.assertEqual(out['text'],'[]')
        response['total_cost_usd']=1.01
        with self.assertRaisesRegex(RuntimeError,'budget'):
            model_adapter.decode_claude_cli(response)
        response['total_cost_usd']=0.012
        response['is_error']=True
        with self.assertRaises(RuntimeError):model_adapter.decode_claude_cli(response)

    def test_cli_env_drops_github_provider_proxy_credentials_and_disables_customizations(self):
        with patch.dict(model_adapter.os.environ,{'HOME':'/synthetic','GH_TOKEN':'not-a-real-secret',
                'ANTHROPIC_API_KEY':'not-a-real-secret','HTTPS_PROXY':'http://synthetic',
                'MODEL_API_KEY':'not-a-real-secret'},clear=True):
            env=model_adapter.claude_cli_environment()
        for key in ['GH_TOKEN','ANTHROPIC_API_KEY','HTTPS_PROXY','MODEL_API_KEY']:
            self.assertNotIn(key,env)
        self.assertEqual(env['CLAUDE_CODE_ENABLE_TELEMETRY'],'0')
        self.assertEqual(env['HOME'],'/synthetic')

    def test_local_attempt_ledger_reserves_before_calls_and_never_overwrites_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger=Path(temp).resolve()/'attempts.jsonl'
            for _ in range(6):model_adapter.reserve_claude_attempt(ledger)
            with self.assertRaisesRegex(RuntimeError,'six'):
                model_adapter.reserve_claude_attempt(ledger)
            self.assertEqual(len(ledger.read_text().splitlines()),6)
