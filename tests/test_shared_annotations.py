"""Exercise real SARIF conversion/contract, mocking only external Docker."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from poc import shared_annotations as shared


def finding():
    return {'rule_id':'error-handling-go/continue-after-error', 'severity':'medium',
            'location':{'path':'src/review.go','start_line':7}, 'message':'Return after the error.'}


def annotation():
    return {'path':'src/review.go','start_line':7,'end_line':7,
            'annotation_level':'warning','title':'','message':'Return after the error.'}


class SharedAnnotationTests(unittest.TestCase):
    def execute(self, rows=None, output=None):
        self.calls=[]
        def runtime(command,payload,environment,timeout):
            self.calls.append((command,json.loads(payload),environment,timeout))
            self.assertTrue(Path(environment['HOME']).is_dir())
            return json.dumps(output if output is not None else {
                'annotations':[annotation()] if rows != [] else [],
                'counts':{'blocking':0,'advisory':0 if rows == [] else 1}}).encode()
        with patch.object(shared.Path,'is_file',return_value=True), patch.object(shared,'_docker',side_effect=runtime):
            return shared.render_annotations([finding()] if rows is None else rows)

    def test_real_conversion_uses_pinned_shared_function_with_advisory_sarif(self):
        rows=[finding()];before=copy.deepcopy(rows)
        self.assertEqual(self.execute(rows),[annotation()]);self.assertEqual(rows,before)
        command,sarif,environment,timeout=self.calls[0]
        result=sarif['runs'][0]['results'][0]
        self.assertEqual(result['properties'],{'gate':'advisory','normalized_severity':'Medium','description':finding()['message']})
        self.assertEqual(result['locations'][0]['physicalLocation']['region'],{'startLine':7})
        self.assertIn('from report_annotations import _annotations_from_sarif',command[-1])
        self.assertIn('/opt/ci-common',command[-1]);self.assertIn(shared.IMAGE,command)
        self.assertIn('@sha256:8624b45376876d905d09d176a77578add5a923df4f8982f1917ec7e3b7a16af3',shared.IMAGE)

    def test_credentials_and_network_mounts_are_absent_and_home_is_fresh(self):
        with patch.dict(os.environ,{'GH_TOKEN':'secret','MODEL_API_KEY':'secret','DOCKER_HOST':'tcp://bad','DOCKER_CONFIG':'bad'}):
            self.execute();first=self.calls[0];self.execute();second=self.calls[0]
        command,_,env,_=first
        self.assertEqual(set(env),{'PATH','HOME','DOCKER_CONFIG','LANG'})
        self.assertEqual(env['HOME'],env['DOCKER_CONFIG']);self.assertNotEqual(env['HOME'],second[2]['HOME'])
        self.assertFalse(Path(env['HOME']).exists())
        for flag in ('--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=32','--memory=256m','--pull=never'):
            self.assertIn(flag,command)
        self.assertFalse(any(item.startswith(('--volume','--mount','--env-file')) for item in command))
        self.assertEqual(command.count('--env'),1)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1',command)

    def test_empty_batch_is_real_shared_conversion_and_malformed_input_fails_before_runtime(self):
        self.assertEqual(self.execute([]),[])
        for rows in (None,{},[{}],[{**finding(),'location':{'path':'../x','start_line':1}}],
                     [{**finding(),'location':{'path':'a\\b','start_line':1}}],
                     [{**finding(),'location':{'path':'a.go','start_line':True}}],
                     [{**finding(),'message':'x'*65537}],[finding()]*51):
            with self.subTest(rows=str(rows)[:50]), patch.object(shared,'_docker') as runtime:
                with self.assertRaises(ValueError): shared.render_annotations(rows)
                runtime.assert_not_called()

    def test_changed_shared_contract_and_fabricated_booleans_fail_closed(self):
        good={'annotations':[annotation()],'counts':{'blocking':0,'advisory':1}}
        bad=[]
        for field,value in [('path','other.go'),('start_line',True),('end_line',True),('annotation_level','failure'),('message','changed')]:
            row=copy.deepcopy(good);row['annotations'][0][field]=value;bad.append(row)
        for value in ({'blocking':False,'advisory':True},{'blocking':1,'advisory':1}):
            row=copy.deepcopy(good);row['counts']=value;bad.append(row)
        bad += [{'annotations':[],'counts':good['counts']}, {**good,'extra':1}, []]
        for output in bad:
            with self.subTest(output=output),self.assertRaises(ValueError):self.execute(output=output)


class DockerBoundaryTests(unittest.TestCase):
    def command(self,code):
        import sys
        return [sys.executable,'-c',code]

    def test_bounded_process_captures_both_streams_without_deadlock(self):
        raw=shared._docker(self.command('import sys; data=sys.stdin.buffer.read();sys.stderr.write("note");sys.stdout.buffer.write(data)'),b'{}',{'PATH':'/usr/bin:/bin'},2)
        self.assertEqual(raw,b'{}')

    def test_timeout_and_output_flood_cleanup_only_the_owned_container(self):
        for code,timeout in [('import time;time.sleep(10)',0.05),
                             ('import sys;sys.stdout.write("x"*2000000)',2),
                             ('import sys;sys.stderr.write("x"*2000000)',2),
                             ('raise SystemExit(3)',2)]:
            with self.subTest(code=code),patch.object(shared,'_cleanup') as cleanup:
                with self.assertRaises(ValueError):shared._docker(self.command(code),b'{}',{'PATH':'/usr/bin:/bin'},timeout,name='review-annotations-own')
                cleanup.assert_called_once_with('review-annotations-own',{'PATH':'/usr/bin:/bin'})

    def test_cleanup_has_exact_name_bounded_time_and_no_inherited_credentials(self):
        env={'HOME':'/tmp/own','DOCKER_CONFIG':'/tmp/own','PATH':'/usr/bin:/bin'}
        with patch.object(shared.subprocess,'run') as run:
            shared._cleanup('review-annotations-own',env)
        args,kwargs=run.call_args
        self.assertEqual(args[0],['/usr/bin/docker','rm','--force','review-annotations-own'])
        self.assertEqual(kwargs['env'],env);self.assertEqual(kwargs['timeout'],5)
        self.assertEqual(kwargs['stdout'],subprocess.DEVNULL);self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
