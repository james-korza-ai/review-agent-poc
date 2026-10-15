"""Offline contract tests; constructed provider responses are not review trials."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poc import check_runner, model_adapter, score

RULE = 'error-handling-go/continue-after-error'


def finding(path='review.go', line=3):
    return {'rule_id': RULE, 'skill': 'error-handling-go', 'severity': 'medium',
            'location': {'path': path, 'start_line': line, 'end_line': line,
                         'start_col': 0, 'end_col': 0},
            'message': 'Value is used after failure.',
            'suggestion': {'text': 'Return on error.', 'replacement': ''}}


def receipt(findings=None):
    return {'text': json.dumps([] if findings is None else findings),
            'raw_response': {'synthetic': True}, 'model': 'gemini/test-version-001',
            'duration_ms': 12, 'tokens': {'in': 100, 'out': 20},
            'cost_usd': 0.002, 'receipt_basis': 'synthetic test receipt'}


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'bug_01.go').write_text('package main\nfunc main() {\n println(0)\n}\n')
        (self.root / 'change.diff').write_text('diff --git a/bug_01.go b/bug_01.go\n--- a/bug_01.go\n+++ b/bug_01.go\n@@ -1 +1,4 @@\n package main\n+func main() {\n+ println(0)\n+}\n')
        self.request = {'repo': {'root': str(self.root)},
                        'diff': {'location': 'change.diff', 'changed_files': [
                            {'path': 'bug_01.go', 'status': 'modified'}]},
                        'budget': {'timeout_seconds': 20, 'max_tokens': 500}}
        self.skill = {'metadata': {'name': RULE, 'severity': 'medium'},
                      'prompt': 'Trusted rule instructions.', 'digest': 'sha256:test'}

    def run_check(self, response):
        with patch.object(check_runner.model_adapter, 'call_model', return_value=response):
            return check_runner.run_check(self.request, self.skill)

    def test_runner_adds_controller_fields_and_maps_blinded_path(self):
        result = self.run_check(receipt([finding()]))
        self.assertEqual(result['findings'][0]['location']['path'], 'bug_01.go')
        self.assertEqual(result['findings'][0]['id'], 'finding-1')
        self.assertIsNone(result['findings'][0]['confidence'])
        self.assertIs(result['findings'][0]['enforcing'], False)
        self.assertEqual(result['dropped'], 0)
        self.assertEqual(result['provenance']['tokens'], {'in': 100, 'out': 20})

    def test_context_blinds_diff_headers_and_numbers_whole_source(self):
        contexts = []
        def fake(prompt, context, **kwargs):
            contexts.append(context)
            return receipt()
        with patch.object(check_runner.model_adapter, 'call_model', side_effect=fake):
            check_runner.run_check(self.request, self.skill)
        text = json.dumps(contexts[0])
        self.assertNotIn('bug_01', text)
        self.assertNotIn(str(self.root), text)
        self.assertIn('review.go', text)
        self.assertIn('3:  println(0)', text)

    def test_runner_drops_invalid_schema_locations_and_control_fields(self):
        bad = []
        for path in ['../review.go', '/review.go', 'other.go', './review.go', 'bug_01.go']:
            bad.append(finding(path))
        for line in [0, 5, True]:
            bad.append(finding(line=line))
        for key, value in [('confidence', 1), ('enforcing', True), ('id', 'injected')]:
            row = finding(); row[key] = value; bad.append(row)
        row = finding(); row['location']['end_line'] = 2; bad.append(row)
        row = finding(); row['rule_id'] = 'other'; bad.append(row)
        result = self.run_check(receipt(bad + [finding()]))
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['dropped'], len(bad))

    def test_runner_rejects_non_array_or_malformed_json(self):
        for text in ['{}', '```json\n[]\n```', '[', 'null']:
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                response = receipt(); response['text'] = text; self.run_check(response)

    def test_runner_requires_receipts_and_cost_basis(self):
        for field in ['model', 'duration_ms', 'tokens', 'cost_usd', 'receipt_basis']:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                response = receipt(); del response[field]; self.run_check(response)
        for value in [None, float('nan'), -1, True]:
            with self.subTest(cost=value), self.assertRaises(RuntimeError):
                response = receipt(); response['cost_usd'] = value; self.run_check(response)

    def test_runner_rejects_escape_symlink_and_duplicate_changed_paths_before_call(self):
        (self.root / 'alias.go').symlink_to(self.root / 'bug_01.go')
        for paths in [['../outside.go'], ['alias.go'], ['bug_01.go', 'bug_01.go']]:
            self.request['diff']['changed_files'] = [{'path': p, 'status': 'modified'} for p in paths]
            with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                check_runner.run_check(self.request, self.skill)

    def test_runner_malformed_path_is_operational_failure(self):
        self.request['diff']['changed_files'][0]['path'] = 7
        with self.assertRaises(RuntimeError):
            check_runner.run_check(self.request, self.skill)

    def test_standalone_runner_rejects_symlink_data_root(self):
        alias = self.root / 'data-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.request['repo']['root'] = str(alias)
        with self.assertRaises(RuntimeError):
            self.run_check(receipt())

    def test_default_adapter_refuses_before_reading_environment_or_network(self):
        with patch.object(model_adapter.os, 'environ', {}), self.assertRaisesRegex(RuntimeError, 'disabled'):
            model_adapter.call_model('prompt', {}, timeout_seconds=1, max_tokens=100)

    def test_live_adapter_refuses_without_verified_provider_spend_enforcement(self):
        with patch.dict(model_adapter.os.environ, {'MODEL_PROVIDER': 'gemini'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'spend'):
                model_adapter.call_model('prompt', {}, timeout_seconds=1, max_tokens=100, allow_live=True)

    def test_gemini_decode_measures_usage_and_requires_cost_receipt(self):
        raw = {'modelVersion': 'test-version-001', 'candidates': [
            {'finishReason': 'STOP', 'content': {'parts': [{'text': '[]'}]}}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 20, 'totalTokenCount': 120}}
        with self.assertRaisesRegex(RuntimeError, 'cost'):
            model_adapter.decode_gemini(raw, duration_ms=5, cost_receipt=None)
        decoded = model_adapter.decode_gemini(raw, duration_ms=5,
            cost_receipt={'cost_usd': 0.002, 'basis': 'explicit synthetic test receipt'})
        self.assertEqual(decoded['model'], 'gemini/test-version-001')
        self.assertEqual(decoded['tokens'], {'in': 100, 'out': 20})
        self.assertEqual(decoded['text'], '[]')
        for mutation in ['usage', 'finish', 'model']:
            changed = copy.deepcopy(raw)
            if mutation == 'usage': del changed['usageMetadata']
            if mutation == 'finish': changed['candidates'][0]['finishReason'] = 'MAX_TOKENS'
            if mutation == 'model': del changed['modelVersion']
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                model_adapter.decode_gemini(changed, duration_ms=5,
                    cost_receipt={'cost_usd': 0.002, 'basis': 'synthetic'})

    def test_transport_bounds_total_tokens_and_sends_key_only_in_header(self):
        requests = []
        class OfflineEndpoint:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return io.BytesIO(b'{"synthetic": true}')
        with patch.object(model_adapter.urllib.request, 'build_opener', return_value=OfflineEndpoint()):
            raw, _ = model_adapter._gemini_transport('rule', {'source': 'package main'},
                endpoint='https://generativelanguage.googleapis.com/v1beta/models/test-001:generateContent',
                model='test-001', key='synthetic-private-key', timeout_seconds=2, max_tokens=1000)
        self.assertEqual(raw, {'synthetic': True})
        request, timeout = requests[0]
        body = json.loads(request.data)
        self.assertEqual(timeout, 2)
        self.assertLessEqual(len(request.data) + body['generationConfig']['maxOutputTokens'], 1000)
        self.assertNotIn('synthetic-private-key', request.full_url + request.data.decode())
        self.assertEqual(request.get_header('X-goog-api-key'), 'synthetic-private-key')
        self.assertEqual(body['systemInstruction']['parts'][0]['text'], 'rule')

    def test_transport_rejects_egress_and_tiny_budget_before_network(self):
        for endpoint in ['http://localhost/model', 'https://evil.example/model',
                         'https://generativelanguage.googleapis.com/v1beta/models/test-001:generateContent?key=leak']:
            with self.subTest(endpoint=endpoint), self.assertRaises(RuntimeError):
                model_adapter._gemini_transport('rule', {}, endpoint=endpoint, model='test-001',
                    key='synthetic', timeout_seconds=1, max_tokens=1000)
        with self.assertRaises(RuntimeError):
            model_adapter._gemini_transport('rule', {'source': 'a' * 2000},
                endpoint='https://generativelanguage.googleapis.com/v1beta/models/test-001:generateContent',
                model='test-001', key='synthetic', timeout_seconds=1, max_tokens=100)


class ScoreTests(unittest.TestCase):
    def report(self, case='bug_01', run=1, rows=None, complete=True):
        return {'case': case, 'run': run, 'findings': [] if rows is None else rows,
                'verdict': {'status': 'pass' if complete else 'error'},
                'skills_run': [{'status': 'ok' if complete else 'error'}],
                'provenance': receipt()}

    def test_exact_start_line_deduplicates_and_wrong_range_is_false_flag_and_miss(self):
        right = finding('bug_01.go', 3)
        output = score.score([self.report(rows=[right, copy.deepcopy(right)])],
                             [{'rule_id': RULE, 'path': 'bug_01.go', 'line': 3}])
        self.assertEqual(output['pooled']['hits'], 1)
        self.assertEqual(output['pooled']['false_flags'], 0)
        wrong = finding('bug_01.go', 2); wrong['location']['end_line'] = 3
        output = score.score([self.report(rows=[wrong])],
                             [{'rule_id': RULE, 'path': 'bug_01.go', 'line': 3}])
        self.assertEqual(output['pooled']['hits'], 0)
        self.assertEqual(output['pooled']['misses'], 1)
        self.assertEqual(output['pooled']['false_flags'], 1)

    def test_missing_and_incomplete_trials_never_become_successes(self):
        output = score.score([self.report(complete=False)], [])
        self.assertEqual(output['coverage']['planned'], 18)
        self.assertEqual(output['coverage']['completed'], 0)
        self.assertEqual(output['coverage']['unfinished'], 18)
        self.assertEqual(len(output['coverage']['missing_trial_ids']), 17)
        self.assertEqual(output['coverage']['incomplete_trial_ids'], ['bug_01/run-1'])
        self.assertIsNone(output['pooled']['precision'])
        self.assertIsNone(output['per_run'][0]['hits'])

    def test_missing_receipts_make_trial_incomplete_and_totals_unknown(self):
        report = self.report(case='ok_03'); del report['provenance']['cost_usd']
        output = score.score([report], [])
        self.assertEqual(output['coverage']['completed'], 0)
        self.assertIsNone(output['observed']['cost_usd'])
        self.assertEqual(output['observed']['unavailable_cost_receipts'], 1)

    def test_duplicate_trial_ids_and_cross_model_pooling_are_rejected(self):
        with self.assertRaises(ValueError):
            score.score([self.report(), self.report()], [])
        other = self.report(run=2); other['provenance']['model'] = 'gemini/another-version'
        with self.assertRaises(ValueError):
            score.score([self.report(), other], [])

    def test_cli_counts_diagnostics_only_trial_as_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / 'runs' / 'bug_01' / 'run-1'
            trial.mkdir(parents=True)
            (trial / 'diagnostics.json').write_text('{"status":"operational_failure"}')
            (root / 'key.json').write_text('[]')
            with contextlib.redirect_stdout(io.StringIO()):
                score.main(['--results', str(root / 'runs'), '--answer-key', str(root / 'key.json'),
                            '--output', str(root / 'score.json')])
            output = json.loads((root / 'score.json').read_text())
            self.assertEqual(output['coverage']['incomplete_trial_ids'], ['bug_01/run-1'])
            self.assertEqual(len(output['coverage']['missing_trial_ids']), 17)

    def test_malformed_receipt_shapes_are_unfinished(self):
        for provenance in [None, {'tokens': None}]:
            report = self.report(); report['provenance'] = provenance
            with self.subTest(provenance=provenance):
                self.assertEqual(score.score([report], [])['coverage']['completed'], 0)

    def test_direct_score_respects_explicit_quality_exclusion_with_numeric_receipts(self):
        report = self.report(rows=[finding('bug_01.go', 3)])
        report['_quality_exclusion'] = 'simulation'
        output = score.score([report], [{'rule_id': RULE, 'path': 'bug_01.go', 'line': 3}])
        self.assertEqual(output['coverage']['completed'], 0)
        self.assertIsNone(output['per_run'][0]['hits'])
        self.assertEqual(output['per_run'][0]['quality_exclusion'], 'simulation')

    def test_recorded_scorer_excludes_simulation_after_trial_directory_rename(self):
        for marker in ['mode', 'quality_evidence', 'raw_response', 'model']:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trial = root / 'runs' / 'bug_01' / 'run-1'; trial.mkdir(parents=True)
                report = self.report(rows=[finding('bug_01.go', 3)])
                diagnostic = {'receipts': {'cost_basis': 'synthetic', 'raw_model_response': {'candidates': []}}}
                if marker == 'mode': diagnostic['mode'] = 'SIMULATION'
                if marker == 'quality_evidence': diagnostic['quality_evidence'] = False
                if marker == 'raw_response': diagnostic['receipts']['raw_model_response'] = {'SIMULATION': True}
                if marker == 'model': report['provenance']['model'] = 'gemini/SIMULATED-NOT-A-MODEL-RUN'
                (trial / 'result.json').write_text(json.dumps(report))
                (trial / 'result.json.diagnostics.json').write_text(json.dumps(diagnostic))
                (root / 'key.json').write_text(json.dumps([{'rule_id': RULE, 'path': 'bug_01.go', 'line': 3}]))
                with contextlib.redirect_stdout(io.StringIO()):
                    score.main(['--results', str(root / 'runs'), '--answer-key', str(root / 'key.json'),
                                '--output', str(root / 'score.json')])
                output = json.loads((root / 'score.json').read_text())
                self.assertEqual(output['coverage']['completed'], 0)
                self.assertIsNone(output['per_run'][0]['hits'])
                self.assertIsNone(output['observed']['cost_usd'])
                self.assertEqual(output['per_run'][0]['quality_exclusion'], 'simulation')


if __name__ == '__main__':
    unittest.main()
