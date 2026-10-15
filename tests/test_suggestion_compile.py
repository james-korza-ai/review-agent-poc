"""Compile guard tests never execute proposed Go programs."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


class CompileSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.log = self.root / 'calls.jsonl'
        self.tool = self.root / 'trusted-go'
        self.tool.write_text(f'''#!{sys.executable}
import json,os,pathlib,resource,sys,time
with open({str(self.log)!r},'a') as f: f.write(json.dumps({{'args':sys.argv[1:],'env':dict(os.environ),'cwd':os.getcwd(),'address_space':resource.getrlimit(resource.RLIMIT_AS)}})+'\\n')
src=pathlib.Path('review.go').read_text()
if 'SLOW_COMPILER' in src: time.sleep(10)
if sys.argv[1]=='list': print(json.dumps({{'Imports':['fmt'] if 'NEW_IMPORT' in src else []}}))
else:
 if 'SYNTAX_BAD' in src: sys.exit(1)
 pathlib.Path('review.test').write_text('not executed')
''');self.tool.chmod(0o700)
        self.source = 'package sample\nfunc value() int {\n return 1\n}\n'
        self.edit = {'start_line': 3, 'end_line': 3, 'original': ' return 1'}

    def run_guard(self, replacement=' return 2', **extra):
        impl = importlib.import_module('poc.suggestion_compile')
        with patch.dict(os.environ, {'SUGGESTION_GO_BINARY': str(self.tool), 'GH_TOKEN': 'private-gh', 'MODEL_API_KEY': 'private-model', **extra}):
            return impl.compile_suggestion(self.source, self.edit, replacement)

    def test_build_only_uses_temporary_module_and_no_secret_environment(self):
        self.assertTrue(self.run_guard())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([c['args'][:1] for c in calls], [['list'], ['list'], ['test']])
        self.assertIn('-c', calls[-1]['args'])
        self.assertTrue(all(c['cwd'] != str(self.root) for c in calls))
        for call in calls:
            self.assertNotIn('GH_TOKEN', call['env']); self.assertNotIn('MODEL_API_KEY', call['env'])
            self.assertNotEqual(call['cwd'], call['env']['TMPDIR'])
            if sys.platform.startswith('linux'):
                self.assertEqual(call['address_space'], [4294967296, 4294967296])
            self.assertEqual(call['env']['GOMEMLIMIT'], '512MiB')
            for key, value in {'CGO_ENABLED':'0', 'GOTOOLCHAIN':'local', 'GOPROXY':'off', 'GOSUMDB':'off'}.items():
                self.assertEqual(call['env'][key], value)
        self.assertFalse(Path(calls[0]['cwd']).exists())

    def test_compiler_failure_refuses_suggestion(self):
        self.assertFalse(self.run_guard(' SYNTAX_BAD'))

    def test_changed_imports_refuse_before_compile(self):
        self.assertFalse(self.run_guard(' NEW_IMPORT'))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call['args'][0] == 'test' for call in calls))

    def test_invalid_original_does_not_invoke_tool(self):
        self.edit['original'] = 'stale source'
        self.assertFalse(self.run_guard())
        self.assertFalse(self.log.exists())

    def test_timeout_refuses_without_running_program(self):
        impl = importlib.import_module('poc.suggestion_compile')
        with patch.object(impl, 'TIMEOUT_SECONDS', 0.2):
            self.assertFalse(self.run_guard(' SLOW_COMPILER'))

    def test_unicode_separator_uses_only_actual_lf_line_boundaries(self):
        self.source = 'package sample\n// unicode\u2028separator\nfunc value() int {\n return 1\n}\n'
        self.edit = {'start_line':5, 'end_line':5, 'original':' return 1'}
        self.assertFalse(self.run_guard())
        self.assertFalse(self.log.exists())
        self.edit['start_line'] = self.edit['end_line'] = 4
        self.assertTrue(self.run_guard())

    def test_linux_configures_hard_address_space_bound(self):
        impl = importlib.import_module('poc.suggestion_compile')
        with patch.object(impl.sys, 'platform', 'linux'), patch.object(impl.resource, 'setrlimit') as limits:
            impl._limits()
        limits.assert_any_call(impl.resource.RLIMIT_AS, (4294967296, 4294967296))

    def test_missing_tool_is_prose_only(self):
        impl = importlib.import_module('poc.suggestion_compile')
        with patch.dict(os.environ, {'SUGGESTION_GO_BINARY': str(self.root/'missing')}):
            self.assertFalse(impl.compile_suggestion(self.source, self.edit, ' return 2'))
