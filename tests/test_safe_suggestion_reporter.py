"""Reporter edit-safety tests; GitHub is fake, pinned source bytes are real data."""
import base64
import copy
import hashlib
import unittest
from unittest.mock import patch
from test_report_github import GithubReporterTests, HEAD, REPO


class SafeSuggestionReporterTests(GithubReporterTests):
    def setUp(self):
        super().setUp()
        compiler = patch("poc.review_local.compile_suggestion", return_value=True, create=True)
        self.compile_guard = compiler.start(); self.addCleanup(compiler.stop)
        self.lines = ['package fixtures'] + [''] * 19 + ['profile, err := fetch()', 'if err != nil {', '\tlog.Print(err)', '}', 'return profile.Name', '}']
        self.source = '\n'.join(self.lines) + '\n'
        raw = self.source.encode()
        self.blob = {'type': 'file', 'path': 'review.go', 'encoding': 'base64', 'size': len(raw),
                     'sha': hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest(),
                     'content': base64.b64encode(raw).decode()}
        self.patch = '@@ -1 +1,26 @@\n package fixtures\n' + '\n'.join('+' + line for line in self.lines[1:])
        self.suggestion = self.result['findings'][0]['suggestion']
        self.suggestion.update(edit={'start_line': 22, 'end_line': 24, 'original': '\n'.join(self.lines[21:24])},
                               replacement='if err != nil {\n\tlog.Print(err)\n\treturn ""\n}')

    def gh(self, endpoint, *, method='GET', payload=None):
        if endpoint == f'repos/{REPO}/contents/review.go?ref={HEAD}':
            self.calls.append((endpoint, method, payload))
            return copy.deepcopy(self.blob)
        return super().gh(endpoint, method=method, payload=payload)

    def posted_comment(self):
        return next(payload['comments'][0] for endpoint, method, payload in self.calls
                    if method == 'POST' and endpoint.endswith('/reviews'))

    def assert_prose_finding(self):
        self.execute()
        comment = self.posted_comment()
        self.assertEqual(comment['line'], 25)
        self.assertNotIn('```suggestion', comment['body'])
        self.assertIn('Continue after failed fetch', comment['body'])
        self.assertEqual(self.checks[0]['output']['annotations'][0]['start_line'], 25)
        self.assertEqual(self.checks[0]['conclusion'], 'neutral')

    def test_valid_edit_uses_own_range_and_keeps_detection_annotation(self):
        self.execute()
        comment = self.posted_comment()
        self.assertEqual((comment['start_line'], comment['line'], comment['start_side'], comment['side']), (22, 24, 'RIGHT', 'RIGHT'))
        self.assertIn('```suggestion\nif err != nil {\n\tlog.Print(err)\n\treturn ""\n}\n```', comment['body'])
        self.assertIn('line 25', comment['body'])
        self.assertEqual(self.checks[0]['output']['annotations'][0]['start_line'], 25)
        self.assertEqual(self.checks[0]['conclusion'], 'neutral')
        self.assertIn((f'repos/{REPO}/contents/review.go?ref={HEAD}', 'GET', None), self.calls)

    def test_exact_source_mismatch_keeps_prose_finding(self):
        self.suggestion['edit']['original'] += ' changed'
        self.assert_prose_finding()

    def test_missing_edit_does_not_apply_legacy_replacement_at_finding(self):
        del self.suggestion['edit']
        self.assert_prose_finding()
        self.assertFalse(any('/contents/' in endpoint for endpoint, _, _ in self.calls))

    def test_outside_diff_range_keeps_prose_finding(self):
        self.patch = '@@ -21,5 +21,5 @@\n' + '\n'.join(' ' + s for s in self.lines[20:25])
        self.suggestion['edit'] = {'start_line': 20, 'end_line': 22, 'original': '\n'.join(self.lines[19:22])}
        self.assert_prose_finding()

    def test_range_spanning_two_hunks_keeps_prose_finding(self):
        self.patch = '@@ -21,2 +21,2 @@\n' + '\n'.join(' ' + s for s in self.lines[20:22]) + '\n@@ -23,3 +23,3 @@\n' + '\n'.join(' ' + s for s in self.lines[22:25])
        self.assert_prose_finding()

    def test_malformed_or_injecting_edits_keep_detection(self):
        original = copy.deepcopy(self.suggestion)
        mutations = [lambda s: s['edit'].update(start_line=True), lambda s: s['edit'].update(path='other.go'),
                     lambda s: s.update(replacement='```\n@victim'), lambda s: s.update(replacement='~~~\n![x](https://bad)'),
                     lambda s: s.update(replacement='bad\0code'), lambda s: s.update(replacement='x' * 8193)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.calls, self.reviews, self.checks = [], [], []
                self.suggestion.clear(); self.suggestion.update(copy.deepcopy(original)); mutation(self.suggestion)
                self.assert_prose_finding()

    def test_unverifiable_head_blob_keeps_prose_finding(self):
        original = copy.deepcopy(self.blob)
        for mutation in [{'sha': 'c' * 40}, {'type': 'symlink'}, {'path': 'other.go'}, {'encoding': 'none'},
                         {'content': 'invalid-base64!'}, {'size': 1048577}]:
            with self.subTest(mutation=mutation):
                self.calls, self.reviews, self.checks = [], [], []
                self.blob = {**original, **mutation}
                self.assert_prose_finding()

    def test_overlapping_edits_both_become_prose(self):
        second = copy.deepcopy(self.result['findings'][0]); second['id'] = 'finding-2'
        second['suggestion']['edit'] = {'start_line': 23, 'end_line': 24, 'original': '\n'.join(self.lines[22:24])}
        self.result['findings'].append(second); self.result['skills_run'][0]['findings'] = 2
        self.execute()
        comments = next(payload['comments'] for endpoint, method, payload in self.calls if method == 'POST' and endpoint.endswith('/reviews'))
        self.assertEqual(len(comments), 2)
        self.assertTrue(all('```suggestion' not in comment['body'] and comment['line'] == 25 for comment in comments))
        self.assertEqual(len(self.checks[0]['output']['annotations']), 2)

    def test_single_line_edit_omits_multiline_start_fields(self):
        self.suggestion.update(edit={'start_line': 23, 'end_line': 23, 'original': self.lines[22]}, replacement='\treturn ""')
        self.execute(); comment = self.posted_comment()
        self.assertEqual(comment['line'], 23)
        self.assertNotIn('start_line', comment)
        self.assertIn('```suggestion', comment['body'])

    def test_compile_failure_retains_detection_and_prose(self):
        self.compile_guard.return_value = False
        self.assert_prose_finding()

    def test_unicode_separator_cannot_shift_exact_head_line_ranges(self):
        self.lines[1] = '// unicode\u2028separator'
        self.source = '\n'.join(self.lines) + '\n'
        raw = self.source.encode()
        self.blob.update(content=base64.b64encode(raw).decode(), size=len(raw),
                         sha=hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest())
        self.patch = '@@ -21,6 +21,6 @@\n' + '\n'.join(' ' + line for line in self.lines[20:26])
        # This original is line22-24 in the real blob, NOT claimed line23-25.
        self.suggestion['edit'].update(start_line=23, end_line=25)
        self.assert_prose_finding()

    def test_oversized_edit_body_falls_back_to_publishable_prose(self):
        self.result['findings'][0]['message'] = 'x' * 65400
        self.execute()
        comment = self.posted_comment()
        self.assertEqual(comment['line'], 25)
        self.assertNotIn('```suggestion', comment['body'])
        self.assertLessEqual(len(comment['body']), 65536)
        self.assertEqual(self.checks[0]['conclusion'], 'neutral')

    def test_valid_deletion_has_an_empty_suggestion_block(self):
        self.suggestion.update(edit={'start_line':23, 'end_line':23, 'original':self.lines[22]}, replacement='')
        self.execute()
        self.assertIn('```suggestion\n\n```', self.posted_comment()['body'])

    def test_unicode_inside_patch_keeps_correct_lf_edit_range(self):
        self.lines[1] = '// unicode\u2028separator'
        raw = ('\n'.join(self.lines) + '\n').encode()
        self.blob.update(content=base64.b64encode(raw).decode(), size=len(raw),
                         sha=hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest())
        self.patch = '@@ -1 +1,26 @@\n package fixtures\n' + '\n'.join('+' + line for line in self.lines[1:])
        self.execute()
        self.assertEqual((self.posted_comment()['start_line'], self.posted_comment()['line']), (22, 24))
        self.assertIn('```suggestion', self.posted_comment()['body'])

    def test_crlf_blob_keeps_detection_without_attempting_a_code_edit(self):
        raw = self.source.replace('\n', '\r\n').encode()
        self.blob.update(content=base64.b64encode(raw).decode(), size=len(raw),
                         sha=hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest())
        self.assert_prose_finding()
        self.compile_guard.assert_not_called()

    def test_compile_budget_retains_all_findings_after_three_edit_attempts(self):
        template = copy.deepcopy(self.result['findings'][0]); findings = []
        for index, line in enumerate((2, 4, 6, 8), 1):
            finding = copy.deepcopy(template); finding['id'] = f'finding-{index}'
            finding['suggestion'].update(edit={'start_line':line, 'end_line':line, 'original':''}, replacement='// note')
            findings.append(finding)
        self.result['findings'] = findings; self.result['skills_run'][0]['findings'] = 4
        self.execute()
        comments = next(payload['comments'] for endpoint, method, payload in self.calls if method == 'POST' and endpoint.endswith('/reviews'))
        self.assertEqual(len(comments), 4)
        self.assertEqual(sum('```suggestion' in comment['body'] for comment in comments), 3)
        self.assertEqual(self.compile_guard.call_count, 3)
        self.assertEqual(comments[-1]['line'], 25)
        self.assertEqual(len(self.checks[0]['output']['annotations']), 4)

    def test_stale_head_after_blob_read_refuses_every_write(self):
        parent_gh = self.gh
        def gh(endpoint, **kwargs):
            response = parent_gh(endpoint, **kwargs)
            if '/contents/' in endpoint:
                self.pull['head']['sha'] = 'c' * 40
            return response
        self.gh = gh
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.execute()
        self.assertFalse(any(method == 'POST' for _, method, _ in self.calls))


# Reuse fixtures, not the parent's separately collected test suite.
for _name in vars(GithubReporterTests):
    if _name.startswith('test_') and _name not in SafeSuggestionReporterTests.__dict__:
        setattr(SafeSuggestionReporterTests, _name, None)
del GithubReporterTests

if __name__ == '__main__':
    unittest.main()
