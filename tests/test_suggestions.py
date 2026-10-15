"""Optional edits remain data: exact bounded replacements or prose-only findings."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from poc import check_runner,run
from test_brain import finding,receipt
import test_orchestration as orchestration
ROOT=orchestration.ROOT
SOURCE='package fixtures\nfunc use() {\n\tvalue, err := fetch()\n\tif err != nil {\n\t\tlog(err)\n\t}\n\tconsume(value)\n}\n'
ORIGINAL='\tif err != nil {\n\t\tlog(err)\n\t}'
REPLACEMENT='\tif err != nil {\n\t\tlog(err)\n\t\treturn\n\t}'

class SuggestionValidationTests(unittest.TestCase):
    def helper(self):
        try:module=importlib.import_module('poc.suggestions')
        except ModuleNotFoundError:module=None
        self.assertIsNotNone(module,'shared exact-edit validator is missing')
        return module.validate_edit

    def edit(self):return {'start_line':4,'end_line':6,'original':ORIGINAL}

    def test_lf_lines_preserves_unicode_separators_and_terminal_line_policy(self):
        from poc.suggestions import lf_lines
        self.assertEqual(lf_lines('a\u2028b\u0085c\vtext\nnext\n'), ['a\u2028b\u0085c\vtext', 'next'])
        for text, expected in [('', []), ('\n', ['']), ('a\n\n', ['a', '']), ('a', ['a'])]:
            self.assertEqual(lf_lines(text), expected)
        edit={'start_line':1,'end_line':1,'original':'old'}
        self.assertEqual(self.helper()(edit, '\u2028'.join(['x']*51), ['old'])[0], edit)

    def test_exact_multiline_edit_preserves_tabs_and_can_differ_from_detection_anchor(self):
        edit=self.edit();before=copy.deepcopy(edit)
        normalized,reason=self.helper()(edit,REPLACEMENT,SOURCE.splitlines())
        self.assertEqual(normalized,edit);self.assertIsNone(reason);self.assertEqual(edit,before)
        self.assertIsNot(normalized,edit)

    def test_deletion_is_allowed_but_empty_or_newline_only_noop_is_rejected(self):
        helper=self.helper();edit={'start_line':5,'end_line':5,'original':'\t\tlog(err)'}
        self.assertEqual(helper(edit,'',SOURCE.splitlines())[0],edit)
        for replacement in (ORIGINAL,ORIGINAL+'\n'):
            self.assertIsNone(helper(self.edit(),replacement,SOURCE.splitlines())[0])

    def test_bad_coordinates_schema_original_types_and_injection_are_rejected(self):
        helper=self.helper()
        edits=[None,True,[],{}, {'start_line':True,'end_line':6,'original':ORIGINAL},
               {'start_line':0,'end_line':6,'original':ORIGINAL},
               {'start_line':4,'end_line':99,'original':ORIGINAL},
               {'start_line':6,'end_line':4,'original':ORIGINAL},
               {'start_line':4,'end_line':6,'original':ORIGINAL.replace('\t',' ')},
               {'start_line':4,'end_line':6,'original':False},
               {**self.edit(),'path':'other.go'}, {**self.edit(),'extra':1}]
        for edit in edits:
            with self.subTest(edit=edit):
                normalized,reason=helper(edit,REPLACEMENT,SOURCE.splitlines())
                self.assertIsNone(normalized);self.assertIsInstance(reason,str)
        for replacement in (True,None,1,'bad\rtext','bad\x00text','\ud800','```suggestion\nattack','~~~code\nattack','x'*8193,'\n'.join(['line']*51)):
            with self.subTest(replacement=repr(replacement)[:30]):
                self.assertIsNone(helper(self.edit(),replacement,SOURCE.splitlines())[0])

    def test_limits_apply_to_original_range_and_utf8_bytes(self):
        helper=self.helper()
        for source,edit in [(['line']*51,{'start_line':1,'end_line':51,'original':'\n'.join(['line']*51)}),
               (['é'*4097],{'start_line':1,'end_line':1,'original':'é'*4097}),
               (['~~~ fenced'],{'start_line':1,'end_line':1,'original':'~~~ fenced'})]:
            self.assertIsNone(helper(edit,'replacement',source)[0])
        edit={'start_line':1,'end_line':1,'original':'line'}
        self.assertIsNone(helper(edit,'é'*4097,['line'])[0])
        self.assertEqual(helper(edit,'x'*8192,['line'])[0],edit)
        self.assertEqual(helper(edit,'\n'.join(['line']*50),['line'])[0],edit)

class CoreSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root/'case.go').write_text(SOURCE)
        (self.root/'change.diff').write_text('@@ -1 +1,8 @@\n package fixtures\n+ value, err := fetch()\n')
        self.request={'repo':{'root':str(self.root)},'diff':{'location':'change.diff','changed_files':[{'path':'case.go','status':'modified'}]},'budget':{'timeout_seconds':120,'max_tokens':16000}}
        self.skill=run.load_skill(ROOT)

    def execute(self,suggestion):
        row=finding(line=7);row['suggestion']=suggestion
        response=receipt([row]);response['raw_response']={'unchanged':response['text']}
        with patch.object(check_runner.model_adapter,'call_model',return_value=response):
            result=check_runner.run_check(self.request,self.skill)
        return row,result

    def test_valid_edit_survives_with_detection_location_unchanged_and_raw_receipt_preserved(self):
        row,result=self.execute({'text':'Return on error.','replacement':REPLACEMENT,'edit':{'start_line':4,'end_line':6,'original':ORIGINAL}})
        self.assertEqual(result['dropped'],0)
        self.assertEqual(result['suggestions_rejected'],0)
        self.assertEqual(result['findings'][0]['location']['start_line'],7)
        self.assertEqual(result['findings'][0]['suggestion'],row['suggestion'])
        self.assertEqual(json.loads(result['receipts']['raw_model_response']['unchanged'])[0],row)
        self.assertEqual(result['provenance']['engine_version'],'poc-0.2.0')
        self.assertEqual(self.skill['metadata']['version'],'0.5.0')

    def test_unicode_separator_does_not_shift_detection_or_exact_edit(self):
        source=SOURCE.replace('package fixtures', 'package fixtures // unicode\u2028separator')
        (self.root/'case.go').write_text(source)
        row,result=self.execute({'text':'Return safely.','replacement':REPLACEMENT,
            'edit':{'start_line':4,'end_line':6,'original':ORIGINAL}})
        self.assertEqual(result['dropped'],0)
        self.assertEqual(result['findings'][0]['location']['start_line'],7)
        self.assertEqual(result['findings'][0]['suggestion'],row['suggestion'])

    def test_invalid_edit_or_legacy_replacement_keeps_finding_prose_and_clears_patch(self):
        for suggestion in [{'text':'Return safely.','replacement':REPLACEMENT},
              {'text':'Return safely.','replacement':REPLACEMENT,'edit':{'start_line':True,'end_line':6,'original':ORIGINAL}},
              {'text':'Return safely.','replacement':False,'edit':{'start_line':4,'end_line':6,'original':ORIGINAL}},
              {'text':'Return safely.','replacement':'```bad','edit':{'start_line':4,'end_line':6,'original':ORIGINAL}}]:
            with self.subTest(suggestion=suggestion):
                row,result=self.execute(suggestion)
                self.assertEqual(result['dropped'],0)
                self.assertEqual(len(result['findings']),1)
                self.assertEqual(result['findings'][0]['suggestion'],{'text':'Return safely.','replacement':'','edit':None})
                self.assertEqual(result['suggestions_rejected'],1)

    def test_plain_legacy_advice_has_no_patch_and_is_not_counted_rejected(self):
        row,result=self.execute({'text':'Return safely.','replacement':''})
        self.assertEqual(result['dropped'],0)
        self.assertEqual(result['suggestions_rejected'],0)
        self.assertEqual(result['findings'][0]['suggestion']['edit'],None)

    def test_crlf_source_edit_downgrades_to_prose_without_hiding_finding(self):
        (self.root/'case.go').write_bytes(SOURCE.replace('\n','\r\n').encode())
        row,result=self.execute({'text':'Return safely.','replacement':REPLACEMENT,'edit':{'start_line':4,'end_line':6,'original':ORIGINAL}})
        self.assertEqual(result['dropped'],0);self.assertEqual(result['suggestions_rejected'],1)
        self.assertIsNone(result['findings'][0]['suggestion']['edit'])

    def test_run_diagnostic_records_rejected_suggestion_without_quality_drop(self):
        fixture=orchestration.OrchestrationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.build();row=finding(line=5);row['suggestion']={'text':'Return safely.','replacement':'arbitrary legacy patch'}
        with patch.object(check_runner.model_adapter,'call_model',return_value=receipt([row])):
            self.assertEqual(run.run_request(fixture.output/'request.json',fixture.output/'result.json',ROOT),0)
        diagnostic=json.loads((fixture.output/'result.json.diagnostics.json').read_text())
        self.assertEqual(diagnostic['suggestions_rejected'],1)
        self.assertEqual(diagnostic['dropped'],0)
