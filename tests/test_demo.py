"""Local simulation verifies plumbing, never model detection quality."""
import json
from pathlib import Path
import tempfile
import unittest
import demo_local

class DemoTests(unittest.TestCase):
    def test_two_real_git_diffs_flow_through_validator_with_simulated_responses(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / 'demo'
            summary = demo_local.demonstrate(output)
            self.assertEqual(summary['mode'], 'SIMULATION')
            self.assertEqual(summary['live_model_calls'], 0)
            self.assertEqual(summary['quality_trials'], 0)
            self.assertEqual([r['findings'] for r in summary['cases']], [1, 0])
            for case in summary['cases']:
                self.assertEqual(case['verdict'], 'pass')
                request = json.loads((output / case['name'] / 'request.json').read_text())
                self.assertEqual(len(request['repo']['base_sha']), 40)
                self.assertNotEqual(request['repo']['base_sha'], request['repo']['head_sha'])
            with self.assertRaises(FileExistsError):
                demo_local.demonstrate(output)
            self.assertFalse((output / 'results').exists())
