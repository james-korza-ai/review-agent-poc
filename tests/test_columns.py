"""Optional model column hints may be omitted; exact line/schema checks stay strict."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from poc import check_runner, run
from test_brain import finding, receipt
import test_orchestration as orchestration
ROOT=orchestration.ROOT


class ColumnTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root/'bug_02.go').write_bytes((ROOT/'fixtures/bug_02.go').read_bytes())
        (self.root/'change.diff').write_bytes((ROOT/'fixtures/bug_02.diff').read_bytes())
        self.request={'repo':{'root':str(self.root)},'diff':{'location':'change.diff','changed_files':[{'path':'bug_02.go','status':'modified'}]},'budget':{'timeout_seconds':120,'max_tokens':16000}}
        self.skill=run.load_skill(ROOT)

    def execute(self,row):
        response=receipt([row]);response['raw_response']={'unchanged-model-text':response['text']}
        with patch.object(check_runner.model_adapter,'call_model',return_value=response):
            return check_runner.run_check(self.request,self.skill)

    def test_oversized_positive_columns_and_reversed_ranges_become_unspecified(self):
        for start,end in [(1,23),(999,0),(999,999),(15,5)]:
            row=finding(line=23);row['location'].update(start_col=start,end_col=end)
            original=copy.deepcopy(row)
            with self.subTest(start=start,end=end):
                result=self.execute(row)
                self.assertEqual(result['dropped'],0)
                self.assertEqual(result['columns_omitted'],1)
                self.assertEqual(result['findings'][0]['location'],{'path':'bug_02.go','start_line':23,'end_line':23,'start_col':0,'end_col':0})
                self.assertEqual(row,original)
                self.assertEqual(json.loads(result['receipts']['raw_model_response']['unchanged-model-text'])[0],original)
                self.assertEqual(result['provenance']['engine_version'],'poc-0.2.0')

    def test_valid_columns_are_preserved_without_omission(self):
        row=finding(line=23);row['location'].update(start_col=1,end_col=5)
        result=self.execute(row)
        self.assertEqual(result['findings'][0]['location']['start_col'],1)
        self.assertEqual(result['findings'][0]['location']['end_col'],5)
        self.assertEqual(result['columns_omitted'],0)

    def test_invalid_types_negative_columns_path_lines_and_schema_still_drop(self):
        for mutation in [{'start_col':-1},{'end_col':True},{'end_col':'23'},{'path':'other.go'},
                         {'start_line':0},{'end_line':999},{'start_line':24,'end_line':23},{'extra':1}]:
            row=finding(line=23);row['location'].update({'end_col':999,**mutation})
            with self.subTest(mutation=mutation):
                result=self.execute(row)
                self.assertEqual(result['findings'],[])
                self.assertEqual(result['dropped'],1)
                self.assertEqual(result['columns_omitted'],0)

    def test_run_sidecar_records_omissions_without_dropped_quality_exclusion(self):
        fixture=orchestration.OrchestrationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.build()
        row=finding(line=5);row['location'].update(start_col=1,end_col=999)
        with patch.object(check_runner.model_adapter,'call_model',return_value=receipt([row])):
            code=run.run_request(fixture.output/'request.json',fixture.output/'result.json',ROOT)
        self.assertEqual(code,0)
        diagnostic=json.loads((fixture.output/'result.json.diagnostics.json').read_text())
        self.assertEqual(diagnostic['columns_omitted'],1)
        self.assertEqual(diagnostic['dropped'],0)
