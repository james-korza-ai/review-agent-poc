"""Offline suggestion-evaluation checks, never live quality evidence."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poc.score import CASES

SOURCE = 'package fixtures\nfunc value() string {\n return "bad"\n}\n'


class SuggestionEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for case in CASES:
            for number in range(1, 4):
                trial = self.root / 'results' / case / f'run-{number}'
                (trial / 'data').mkdir(parents=True)
                (trial / 'data' / f'{case}.go').write_text(SOURCE)
                findings = []
                if case.startswith('bug_'):
                    findings = [{'location': {'path': f'{case}.go', 'start_line': 3},
                        'suggestion': {'text': 'Use the fallback.', 'replacement': ' return "ok"',
                            'edit': {'start_line': 3, 'end_line': 3, 'original': ' return "bad"'}}}]
                (trial / 'result.json').write_text(json.dumps({'findings': findings}))
                (trial / 'result.json.diagnostics.json').write_text(json.dumps(
                    {'status': 'completed', 'suggestions_rejected': 0}))

    def module(self):
        try:
            return importlib.import_module('poc.evaluate_suggestions')
        except ImportError:
            self.fail('suggestion evaluation stage is missing')

    def run_check(self, compiler=True):
        module = self.module()
        with patch.object(module, 'compile_suggestion', return_value=compiler) as compile_call:
            with contextlib.redirect_stdout(io.StringIO()):
                card = module.verify(self.root)
        return card, compile_call

    def test_all_six_bug_edits_require_exact_source_and_compile_only_guard(self):
        card, compiler = self.run_check()
        self.assertTrue(card['met'])
        self.assertEqual(card['valid_bug_edits'], 6)
        self.assertEqual(card['clean_trials_without_findings'], 12)
        self.assertEqual(compiler.call_count, 6)
        self.assertFalse(card['proposed_code_executed'])
        self.assertEqual(json.loads((self.root / 'suggestions.json').read_text()), card)

    def test_wrong_original_keeps_trial_failed_and_never_compiles_that_edit(self):
        path = self.root / 'results/bug_01/run-1/result.json'
        report = json.loads(path.read_text())
        report['findings'][0]['suggestion']['edit']['original'] = 'different source'
        path.write_text(json.dumps(report))
        card, compiler = self.run_check()
        self.assertFalse(card['met'])
        self.assertEqual(card['valid_bug_edits'], 5)
        self.assertEqual(compiler.call_count, 5)

    def test_compile_rejection_is_not_a_valid_fix(self):
        card, _ = self.run_check(compiler=False)
        self.assertFalse(card['met'])
        self.assertEqual(card['valid_bug_edits'], 0)

    def test_missing_result_or_rejected_suggestion_cannot_pass(self):
        (self.root / 'results/bug_01/run-1/result.json').unlink()
        path = self.root / 'results/bug_02/run-1/result.json.diagnostics.json'
        path.write_text(json.dumps({'status': 'completed', 'suggestions_rejected': 1}))
        card, compiler = self.run_check()
        self.assertFalse(card['met'])
        self.assertEqual(card['valid_bug_edits'], 4)
        self.assertEqual(compiler.call_count, 4)
