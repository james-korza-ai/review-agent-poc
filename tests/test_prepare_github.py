import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = 'james-korza-ai/review-agent-poc'
BASE, HEAD = 'b' * 40, 'a' * 40

class PrepareTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('poc.prepare_github')
        except ImportError:
            self.fail('trusted Actions preparation is missing')

    def event(self):
        return {'action': 'created', 'repository': {'full_name': REPO},
                'issue': {'number': 1, 'pull_request': {}},
                'comment': {'id': 99, 'body': '/review', 'user': {'login': 'james-korza-ai'}}}

    def gh(self, endpoint, **kwargs):
        if endpoint == f'repos/{REPO}':
            return {'full_name': REPO, 'default_branch': 'main', 'owner': {'login': 'james-korza-ai', 'type': 'User'}}
        if endpoint == f'repos/{REPO}/pulls/1':
            return {'number': 1, 'state': 'open', 'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPO}},
                    'head': {'sha': HEAD, 'repo': {'full_name': REPO}}}
        if endpoint == f'repos/{REPO}/issues/comments/99':
            return {'id': 99, 'body': '/review', 'user': {'login': 'james-korza-ai'},
                    'issue_url': f'https://api.github.com/repos/{REPO}/issues/1'}
        raise AssertionError(endpoint)

    def test_exact_owner_command_pins_current_commits(self):
        module = self.module()
        with patch('poc.review_local._gh', side_effect=self.gh):
            pins = module.identify(self.event(), REPO)
        self.assertEqual(pins, {'repo': REPO, 'pr': 1, 'comment_id': 99, 'base_sha': BASE, 'head_sha': HEAD})

    def test_non_pr_or_other_repo_or_non_exact_command_refused(self):
        module = self.module()
        for field in ('pr', 'repo', 'command', 'owner'):
            event = self.event()
            if field == 'pr': del event['issue']['pull_request']
            if field == 'repo': event['repository']['full_name'] = 'other/repo'
            if field == 'command': event['comment']['body'] = '/review please'
            if field == 'owner': event['comment']['user']['login'] = 'other'
            with self.subTest(field=field), patch('poc.review_local._gh', side_effect=self.gh), self.assertRaises(ValueError):
                module.identify(event, REPO)

    def test_only_made_up_review_file_can_enter_model(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for path in ('secret.txt', '.github/workflows/review.yml', 'review_test.go'):
                request = {'request_id': 'id', 'repo': {'base_sha': BASE, 'head_sha': HEAD},
                           'diff': {'changed_files': [{'path': path, 'status': 'modified'}]}}
                with self.subTest(path=path), patch.object(module, 'build_request', return_value=request), self.assertRaises(ValueError):
                    module.prepare({'repo': REPO, 'pr': 1, 'comment_id': 99, 'base_sha': BASE, 'head_sha': HEAD}, out / 'source', out / 'case')

    def test_request_pins_captured_before_model(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {'request_id': 'id', 'repo': {'base_sha': BASE, 'head_sha': HEAD},
                       'diff': {'changed_files': [{'path': 'review.go', 'status': 'modified'}]}}
            with patch.object(module, 'build_request', return_value=request):
                pins = module.prepare({'repo': REPO, 'pr': 1, 'comment_id': 99, 'base_sha': BASE, 'head_sha': HEAD}, root / 'source', root / 'case')
            self.assertEqual(pins['request_id'], 'id')
            self.assertEqual(pins['head_sha'], HEAD)
