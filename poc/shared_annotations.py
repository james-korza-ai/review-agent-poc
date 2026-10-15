"""Reuse one pure function in a pinned public image, never its publishing policy.

The runner pre-pulls IMAGE. No source, credentials, mounts or network enter the
container. Changing this internal shared API pin requires a new contract check.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
import uuid

from .build_request import RULE, relative_path

IMAGE = ('ghcr.io/korzainc/ci-common:0.4.0@sha256:'
         '8624b45376876d905d09d176a77578add5a923df4f8982f1917ec7e3b7a16af3')
MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 45
_BRIDGE = '''import json,sys
sys.path.insert(0, "/opt/ci-common")
from report_annotations import _annotations_from_sarif
annotations,counts = _annotations_from_sarif(json.load(sys.stdin))
json.dump({"annotations":annotations,"counts":counts},sys.stdout)
'''


def _cleanup(name, environment):
    # The unpredictable name belongs to this invocation only. Never prune or
    # enumerate containers. Failure stays closed even if the daemon is down.
    try:
        subprocess.run(['/usr/bin/docker', 'rm', '--force', name],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=environment, timeout=5,
                       check=False, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _docker(command, payload, environment, timeout, *, name=None):
    """Bound all three pipes concurrently; kill client and own container on failure."""
    if name is None and '--name' in command:
        name = command[command.index('--name') + 1]
    child = None
    success = False
    deadline = time.monotonic() + timeout
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=environment,
                                 start_new_session=True)
        output = bytearray()
        errors = 0
        offset = 0
        with selectors.DefaultSelector() as selector:
            for stream, event in ((child.stdin, selectors.EVENT_WRITE),
                                  (child.stdout, selectors.EVENT_READ),
                                  (child.stderr, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('shared renderer timed out')
                for key, _ in selector.select(remaining):
                    stream = key.fileobj
                    if stream is child.stdin:
                        try:
                            offset += os.write(stream.fileno(), payload[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(payload)
                        if offset == len(payload):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                        elif stream is child.stdout:
                            if len(output) + len(chunk) > MAX_BYTES:
                                raise ValueError('shared renderer output too large')
                            output.extend(chunk)
                        else:
                            errors += len(chunk)
                            if errors > 65536:
                                raise ValueError('shared renderer stderr too large')
        if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise ValueError('shared renderer failed')
        success = True
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('shared renderer unavailable') from None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        if not success and name is not None:
            _cleanup(name, environment)


def render_annotations(findings):
    """Return exact shared-rendered warnings or fail before publishing anything."""
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError('unsupported annotation count')
    expected, results = [], []
    try:
        for finding in findings:
            location = finding['location']
            path, line, message = location['path'], location['start_line'], finding['message']
            relative_path(path)
            if (finding['rule_id'] != RULE or finding['severity'] != 'medium'
                    or type(line) is not int or not 1 <= line <= 2147483647
                    or not isinstance(message, str) or not message.strip()
                    or len(message.encode('utf-8')) > 65536):
                raise ValueError('unsupported annotation data')
            expected.append({'path': path, 'start_line': line, 'end_line': line,
                'annotation_level': 'warning', 'title': '', 'message': message})
            results.append({'ruleId': RULE, 'message': {'text': message},
                'locations': [{'physicalLocation': {'artifactLocation': {'uri': path},
                    'region': {'startLine': line}}}],
                'properties': {'gate': 'advisory', 'normalized_severity': 'Medium',
                               'description': message}})
        payload = json.dumps({'version': '2.1.0', 'runs': [{'results': results}]}).encode()
    except (KeyError, TypeError, UnicodeError):
        raise ValueError('unsupported annotation data') from None
    if len(payload) > MAX_BYTES:
        raise ValueError('annotation input too large')
    if not Path('/usr/bin/docker').is_file():
        raise ValueError('pinned shared renderer runtime unavailable')
    name = 'review-annotations-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='review-docker-') as config:
        environment = {'PATH': '/usr/bin:/bin', 'DOCKER_CONFIG': config,
                       'HOME': config, 'LANG': 'C.UTF-8'}
        command = ['/usr/bin/docker', 'run', '--rm', '--name', name, '--pull=never',
            '-i', '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=32', '--memory=256m',
            '--cpus=1', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--entrypoint',
            'python3', IMAGE, '-I', '-c', _BRIDGE]
        encoded = _docker(command, payload, environment, TIMEOUT_SECONDS)
    try:
        rendered = json.loads(encoded)
        counts = rendered['counts']
        rows = rendered['annotations']
        if (type(rendered) is not dict or set(rendered) != {'annotations', 'counts'}
                or type(counts) is not dict or set(counts) != {'blocking', 'advisory'}
                or any(type(value) is not int for value in counts.values())
                or counts != {'blocking': 0, 'advisory': len(findings)}
                or not isinstance(rows, list) or len(rows) != len(expected)):
            raise ValueError('shared renderer contract changed')
        for row, wanted in zip(rows, expected):
            if (type(row) is not dict or type(row.get('start_line')) is not int
                    or type(row.get('end_line')) is not int or row != wanted):
                raise ValueError('shared renderer annotation changed')
        return rows
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError('invalid shared renderer response') from None
