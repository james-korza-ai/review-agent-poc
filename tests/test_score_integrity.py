"""Recorded quality scoring must not turn rejected model output into clean trials."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from poc import score

RULE = 'error-handling-go/continue-after-error'
KEY = [{'rule_id': RULE, 'path': 'bug_01.go', 'line': 25}]


def report(case='bug_01', run=1, findings=None):
    return {'case': case, 'run': run, 'verdict': {'status': 'pass'},
            'findings': [] if findings is None else findings, 'skills_run': [{'status': 'ok'}],
            'provenance': {'model': 'gemini/test-recorded-version', 'duration_ms': 12,
                           'tokens': {'in': 100, 'out': 20}, 'cost_usd': 0.001}}


def correct():
    return {'rule_id': RULE, 'location': {'path': 'bug_01.go', 'start_line': 25}}


class ScoreIntegrityTests(unittest.TestCase):
    def record_and_score(self, dropped):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / 'results' / 'ok_03' / 'run-1'
            trial.mkdir(parents=True)
            (trial / 'result.json').write_text(json.dumps(report('ok_03')))
            (trial / 'result.json.diagnostics.json').write_text(json.dumps({
                'status': 'completed', 'dropped': dropped,
                'receipts': {'raw_model_response': {'constructed-test-receipt': True},
                             'cost_basis': 'Synthetic unit-price estimate, not billing'}}))
            (root / 'key.json').write_text(json.dumps(KEY))
            with contextlib.redirect_stdout(io.StringIO()):
                score.main(['--results', str(root / 'results'), '--answer-key', str(root / 'key.json'),
                            '--output', str(root / 'score.json')])
            return json.loads((root / 'score.json').read_text())

    def test_dropped_malformed_output_cannot_count_as_clean_deliberate_pattern_trial(self):
        output = self.record_and_score(1)
        self.assertEqual(output['coverage']['completed'], 0)
        self.assertEqual(output['coverage']['incomplete_trial_ids'], ['ok_03/run-1'])
        self.assertEqual(output['per_run'][0]['quality_exclusion'], 'dropped_findings')
        self.assertIsNone(output['per_run'][0]['false_flags'])
        self.assertEqual(output['observed']['cost_usd'], 0.001)
        self.assertEqual(output['observed']['tokens'], {'in': 100, 'out': 20})
        self.assertEqual(output['observed']['duration_ms'], 12)

    def test_zero_dropped_findings_retains_real_quiet_trial(self):
        output = self.record_and_score(0)
        self.assertEqual(output['coverage']['completed'], 1)
        self.assertEqual(output['per_run'][0]['false_flags'], 0)

    def test_invalid_dropped_counts_cannot_satisfy_zero_rejections(self):
        for dropped in (-1, True, '0', None):
            with self.subTest(dropped=dropped):
                output = self.record_and_score(dropped)
                self.assertEqual(output['coverage']['completed'], 0)
                self.assertEqual(output['per_run'][0]['quality_exclusion'], 'invalid_dropped_count')

    def test_malformed_finding_excludes_only_its_trial_and_preserves_observed_receipts(self):
        bad = [None, {}, {'rule_id': RULE, 'location': None},
               {'rule_id': RULE, 'location': {'path': 'bug_01.go', 'start_line': True}},
               {'rule_id': RULE, 'location': {'path': 'bug_01.go', 'start_line': 0}}]
        for finding in bad:
            with self.subTest(finding=finding):
                output = score.score([report(findings=[finding]), report(run=2, findings=[correct()])], KEY)
                self.assertEqual(output['coverage']['completed'], 1)
                self.assertEqual(output['coverage']['incomplete_trial_ids'], ['bug_01/run-1'])
                self.assertEqual(output['per_run'][0]['quality_exclusion'], 'malformed_findings')
                self.assertIsNone(output['per_run'][0]['hits'])
                self.assertEqual(output['pooled']['hits'], 1)
                self.assertEqual(output['pooled']['misses'], 0)
                self.assertEqual(output['observed']['cost_usd'], 0.002)
                self.assertEqual(output['observed']['tokens'], {'in': 200, 'out': 40})

    def test_malformed_skill_status_excludes_trial_without_aborting_other_rows(self):
        malformed = report(); malformed['skills_run'] = [None]
        output = score.score([malformed, report(run=2, findings=[correct()])], KEY)
        self.assertEqual(output['coverage']['completed'], 1)
        self.assertEqual(output['pooled']['hits'], 1)

    def test_exact_rule_path_and_line_remain_required_and_duplicate_matches_count_once(self):
        for key, replacement in [('rule_id', 'different/rule'), ('path', 'other.go'), ('start_line', 24)]:
            finding = correct()
            if key == 'rule_id': finding[key] = replacement
            else: finding['location'][key] = replacement
            output = score.score([report(findings=[finding])], KEY)
            with self.subTest(key=key):
                self.assertEqual(output['pooled']['hits'], 0)
                self.assertEqual(output['pooled']['misses'], 1)
                self.assertEqual(output['pooled']['false_flags'], 1)
        output = score.score([report(findings=[correct(), correct()])], KEY)
        self.assertEqual(output['pooled']['hits'], 1)
        self.assertEqual(output['pooled']['false_flags'], 0)
