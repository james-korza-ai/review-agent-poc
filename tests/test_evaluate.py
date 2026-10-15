import importlib
import json
from pathlib import Path
import tempfile
import unittest

class EvaluationTests(unittest.TestCase):
    def module(self):
        try:return importlib.import_module('poc.evaluate')
        except ImportError:self.fail('evaluation runner missing')

    def test_prepare_makes_eighteen_real_git_requests_without_answers(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory).resolve()/'evaluation'
            module.prepare(out)
            requests=list((out/'results').glob('*/run-*/request.json'))
            self.assertEqual(len(requests),18)
            for path in requests:
                request=json.loads(path.read_text())
                self.assertEqual(len(request['diff']['changed_files']),1)
                self.assertEqual(request['diff']['changed_files'][0]['path'],path.parent.parent.name+'.go')
                self.assertNotIn('answer',json.dumps(request).lower())
                self.assertNotEqual(request['repo']['base_sha'],request['repo']['head_sha'])
            self.assertFalse(list(out.glob('**/*key*')))

    def test_pass_bar_requires_all_evidence_not_just_pooled_precision(self):
        module=self.module()
        rows=[{'case':c,'completed':True,'hits':int(c.startswith('bug')),'misses':0,'false_flags':0,
               'provenance':{'duration_ms':10,'tokens':{'in':10,'out':10},'cost_usd':.001}}
              for c in module.CASES for n in range(3)]
        score={'per_run':rows,'coverage':{'completed':18},'observed':{'invalid_receipts':0,'unavailable_cost_receipts':0}}
        bar=module.pass_bar(score)
        self.assertTrue(bar['met'])
        self.assertEqual(bar['status'],'provisional, not team-agreed')
        rows[8]['false_flags']=1
        self.assertFalse(module.pass_bar(score)['met'])
        rows[8]['false_flags']=0
        rows[0]['completed']=False
        self.assertFalse(module.pass_bar(score)['met'])
        rows[0]['completed']=True
        rows[0]['provenance']['duration_ms']=121000
        self.assertFalse(module.pass_bar(score)['met'])
