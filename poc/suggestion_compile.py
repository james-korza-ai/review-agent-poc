"""Bounded one-file Go compile check; never run the proposed program or tests."""
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .suggestions import lf_lines, validate_edit

TIMEOUT_SECONDS = 30
MAX_SOURCE_BYTES = 1024 * 1024


def _limits():
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def _go(binary, args, directory, environment, deadline):
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen([binary, *args], cwd=directory, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                 start_new_session=True, preexec_fn=_limits)
        try:
            code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            return None
        output.seek(0)
        raw = output.read(MAX_SOURCE_BYTES + 1)
        return raw if code == 0 and len(raw) <= MAX_SOURCE_BYTES else None


def compile_suggestion(source_text, edit, replacement):
    """Return False for unsupported/tool/build failures; caller retains prose.

    SUGGESTION_GO_BINARY is a trusted controller setting, never a model field.
    The child receives no inherited GitHub/model/SSH credentials or Go settings.
    Linux adds a hard 4GiB address-space cap. Other Unix hosts retain the
    portable limits and soft GOMEMLIMIT; no hard memory bound is claimed there.
    Import equality and compilation are guards, not semantic approval of a fix.
    """
    if not isinstance(source_text, str) or '\r' in source_text:
        return False
    try:
        if len(source_text.encode('utf-8')) > MAX_SOURCE_BYTES:
            return False
        lines = lf_lines(source_text)
        normalized, _ = validate_edit(edit, replacement, lines)
        if normalized is None:
            return False
        binary = os.environ.get('SUGGESTION_GO_BINARY') or shutil.which('go')
        if not binary or not Path(binary).is_absolute() or not Path(binary).is_file():
            return False
        binary = str(Path(binary).resolve())
        # An executable from the data/engine checkout is not a toolchain.
        try:
            Path(binary).relative_to(Path(__file__).resolve().parents[1])
            return False
        except ValueError:
            pass
        # Work with source offsets, so untouched prefix/suffix bytes and the
        # replaced range's final line terminator remain exactly as supplied.
        parts = source_text.split('\n')
        source_lines = [line + '\n' for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        start, end = normalized['start_line'], normalized['end_line']
        ending = '\n' if source_lines[end - 1].endswith('\n') else ''
        replacement_body = replacement.removesuffix('\n')
        patched = ''.join(source_lines[:start - 1]) + replacement_body + (ending if replacement else '') + ''.join(source_lines[end:])
        if len(patched.encode('utf-8')) > MAX_SOURCE_BYTES:
            return False
        deadline = time.monotonic() + TIMEOUT_SECONDS
        with tempfile.TemporaryDirectory(prefix='review-suggestion-compile-') as folder:
            directory = Path(folder) / 'module'
            directory.mkdir()
            scratch = Path(folder) / 'tmp'
            scratch.mkdir()
            environment = {'PATH': f'{Path(binary).parent}:/usr/bin:/bin', 'HOME': folder, 'TMPDIR': str(scratch),
                           'CGO_ENABLED': '0', 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOSUMDB': 'off',
                           'GOVCS': 'off', 'GOENV': 'off', 'GOWORK': 'off', 'GOFLAGS': '',
                           'GOCACHE': str(Path(folder) / 'cache'), 'GOMODCACHE': str(Path(folder) / 'modules'),
                           'GOPATH': str(Path(folder) / 'gopath'), 'GOMAXPROCS': '2', 'GOMEMLIMIT': '512MiB'}
            (directory / 'go.mod').write_text('module review.invalid/suggestion\n\ngo 1.20\n')
            source = directory / 'review.go'
            imports = []
            for content in (source_text, patched):
                source.write_text(content)
                raw = _go(binary, ['list', '-e', '-json', '.'], str(directory), environment, deadline)
                if raw is None:
                    return False
                metadata = json.loads(raw)
                direct = metadata.get('Imports', [])
                if not isinstance(direct, list) or any(not isinstance(item, str) for item in direct):
                    return False
                imports.append(sorted(direct))
            if imports[0] != imports[1]:
                return False
            return _go(binary, ['test', '-c', '-p', '1', '-vet=off', '-o', str(directory / 'review.test'), '.'],
                       str(directory), environment, deadline) is not None
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        return False
