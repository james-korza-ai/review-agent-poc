"""Model adapters: offline Gemini preparation and an explicit local CLI demo.

Gemini live calls remain blocked on provider controls. The separately authorized
Claude CLI demo uses normal local sign-in, not a copied key or hosted CI secret.
REST shape: https://ai.google.dev/api/generate-content
"""
import json
import math
import os
import re
import time
import subprocess
import fcntl
import signal
from pathlib import Path
import urllib.error
import urllib.request


class OperationalError(RuntimeError):
    """The check did not finish; never convert this into a passing review."""


def nonnegative_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_receipt(receipt, *, require_basis=True):
    if not isinstance(receipt, dict):
        raise OperationalError('Missing model receipt')
    model = receipt.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'(?:gemini|claude-cli)/[A-Za-z0-9._-]+', model):
        raise OperationalError('Missing actual provider/model receipt')
    if not nonnegative_number(receipt.get('duration_ms')):
        raise OperationalError('Missing duration receipt')
    tokens = receipt.get('tokens')
    if not isinstance(tokens, dict) or any(type(tokens.get(k)) is not int or tokens[k] < 0 for k in ('in', 'out')):
        raise OperationalError('Missing token receipt')
    if not nonnegative_number(receipt.get('cost_usd')):
        raise OperationalError('Missing cost receipt; unknown cost is not zero')
    if require_basis and (not isinstance(receipt.get('receipt_basis'), str) or not receipt['receipt_basis'].strip()):
        raise OperationalError('Missing cost receipt basis')


def decode_gemini(raw, *, duration_ms, cost_receipt):
    """Decode measured usage; Gemini does not supply a USD billing receipt.

    cost_receipt must come from an independently established cost mechanism,
    never model prose. No such mechanism is enabled by the current live gate.
    """
    if not isinstance(cost_receipt, dict) or not nonnegative_number(cost_receipt.get('cost_usd')) or not isinstance(cost_receipt.get('basis'), str) or not cost_receipt['basis'].strip():
        raise OperationalError('Missing cost receipt and basis')
    try:
        candidates = raw['candidates']
        if len(candidates) != 1 or candidates[0]['finishReason'] != 'STOP':
            raise OperationalError('Model response unfinished or blocked')
        parts = candidates[0]['content']['parts']
        if not parts or any(set(p) != {'text'} or not isinstance(p['text'], str) for p in parts):
            raise OperationalError('Unexpected non-text model response')
        usage = raw['usageMetadata']
        prompt, output, total = (usage[k] for k in ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount'))
        if any(type(n) is not int or n < 0 for n in (prompt, output, total)) or total < prompt + output:
            raise OperationalError('Invalid token receipt')
        # Total includes thinking/other generated tokens, unlike candidate text alone.
        decoded = {'text': ''.join(p['text'] for p in parts), 'raw_response': raw,
                   'model': 'gemini/' + raw['modelVersion'], 'duration_ms': duration_ms,
                   'tokens': {'in': prompt, 'out': total - prompt},
                   'cost_usd': cost_receipt['cost_usd'], 'receipt_basis': cost_receipt['basis']}
        validate_receipt(decoded)
        return decoded
    except (KeyError, TypeError, ValueError, IndexError):
        raise OperationalError('Malformed or missing Gemini response receipt') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperationalError('Model endpoint redirect refused')


def _gemini_transport(prompt, context, *, endpoint, model, key, timeout_seconds, max_tokens):
    """Single HTTP attempt; caller must supply already approved runtime policy."""
    if not re.fullmatch(r'[A-Za-z0-9._-]+', model) or model in ('latest', 'auto'):
        raise OperationalError('Explicit model ID required')
    expected = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent'
    if endpoint != expected:
        raise OperationalError('Unsupported model endpoint; exact official Gemini model URL required')
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or type(max_tokens) is not int or max_tokens < 1:
        raise OperationalError('Positive timeout and token budget required')
    body = {'systemInstruction': {'parts': [{'text': prompt}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(context, ensure_ascii=True)}]}],
            'generationConfig': {'maxOutputTokens': max_tokens, 'responseMimeType': 'application/json', 'candidateCount': 1}}
    # Reserve output tokens from the same total ceiling. Bytes are a conservative
    # upper bound here, not a claimed measured tokenizer receipt.
    input_ceiling = len(json.dumps(body).encode('utf-8'))
    output_ceiling = max_tokens - input_ceiling
    if output_ceiling < 1:
        raise OperationalError('Context exceeds conservative token budget; split input before live use')
    body['generationConfig']['maxOutputTokens'] = output_ceiling
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode('utf-8'),
                                     headers={'Content-Type': 'application/json', 'x-goog-api-key': key}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    started = time.monotonic()
    try:
        # Sole external model HTTP call; no retries or fallback transport.
        with opener.open(request, timeout=timeout_seconds) as response:
            encoded = response.read(8 * 1024 * 1024 + 1)
        if len(encoded) > 8 * 1024 * 1024:
            raise OperationalError('Model response exceeds receipt size limit')
        duration = (time.monotonic() - started) * 1000
        if duration > timeout_seconds * 1000:
            raise OperationalError('Model response exceeded timeout')
        return json.loads(encoded), duration
    except (urllib.error.URLError, OSError, ValueError):
        # Exception/body/URL details may contain credentials; never echo them.
        raise OperationalError('Model transport failed; attempt unfinished') from None


def call_model(prompt, context, *, timeout_seconds, max_tokens, allow_live=False):
    if not allow_live:
        raise OperationalError('Live model calls disabled; no recorded review was made')
    provider = os.environ.get('MODEL_PROVIDER')
    if provider == 'claude-cli':
        return _claude_cli(prompt, context, timeout_seconds=timeout_seconds, max_tokens=max_tokens)
    if provider != 'gemini':
        raise OperationalError('Only Gemini REST is implemented; provider must be selected explicitly')
    # A flag or local run counter cannot establish a provider-enforced hard cap.
    # Keep this gate until the operator's actual provider mechanism and billing
    # receipts are implemented and independently verified. No key is read here.
    raise OperationalError('Provider spend enforcement and cost receipt acquisition are unimplemented; live call refused')


def claude_cli_environment():
    """Use ordinary CLI sign-in; never pass GitHub/API/proxy secrets to the model."""
    environment = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
                   if k in os.environ}
    environment.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                       CLAUDE_CODE_ENABLE_TELEMETRY='0',
                       OTEL_LOGS_EXPORTER='none', OTEL_METRICS_EXPORTER='none',
                       OTEL_TRACES_EXPORTER='none')
    return environment


def reserve_claude_attempt(path):
    """Six CLI attempts maximum, including failures; not a provider hard cap."""
    from .build_request import checked_path
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        lines = handle.read().splitlines()
        if len(lines) >= 6:
            raise OperationalError('Local demo is limited to six CLI attempts, including failures')
        for line in lines:
            if json.loads(line).get('kind') != 'cli_attempt':
                raise OperationalError('Invalid local attempt ledger')
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps({'kind': 'cli_attempt', 'number': len(lines) + 1,
                                 'per_invocation_budget_usd': 1,
                                 'budget_kind': 'CLI limit, not provider hard cap'}) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def decode_claude_cli(raw):
    """CLI usage is measured; reported list-price cost is an estimate, not a bill."""
    if not isinstance(raw, dict):
        raise OperationalError('Malformed Claude CLI receipt')
    try:
        if raw.get('subtype') != 'success' or raw.get('is_error') is not False or raw.get('num_turns') != 1:
            raise OperationalError('Claude CLI did not complete a single review turn')
        models = raw['modelUsage']
        if not isinstance(models, dict) or set(models) != {'claude-opus-5-5'}:
            raise OperationalError('Unexpected model usage; no single-model receipt claimed')
        usage = raw['usage']
        counts = [usage[k] for k in ('input_tokens', 'cache_creation_input_tokens',
                                     'cache_read_input_tokens', 'output_tokens')]
        if any(type(v) is not int or v < 0 for v in counts):
            raise OperationalError('Missing Claude CLI usage')
        if not isinstance(raw['result'], str):
            raise OperationalError('Missing Claude CLI text result')
        decoded = {'text': raw['result'], 'raw_response': raw,
                   'model': 'claude-cli/claude-opus-5-5', 'duration_ms': raw['duration_ms'],
                   'tokens': {'in': sum(counts[:3]), 'out': counts[3]},
                   'cost_usd': raw['total_cost_usd'],
                   'receipt_basis': 'Claude CLI reported list-price estimate; not billed cost or provider-enforced spend control'}
        validate_receipt(decoded)
        if decoded['cost_usd'] > 1:
            raise OperationalError('Claude CLI reported over-budget usage; review is unfinished')
        return decoded
    except (KeyError, TypeError, ValueError):
        raise OperationalError('Malformed Claude CLI receipt; no success claimed') from None


def run_claude_process(args, cwd, environment, payload, timeout):
    """Bound cleanup of this owned process group, including pipe-holding children."""
    child = subprocess.Popen(args, cwd=cwd, env=environment, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = child.communicate(payload, timeout=timeout)
        return child.returncode, stdout
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=5)
        raise OperationalError('Claude CLI timed out; owned process group stopped') from None


def _claude_cli(prompt, context, *, timeout_seconds, max_tokens):
    """One local CLI invocation; no tools, hooks, plugins or persisted session."""
    from .build_request import checked_path
    if type(max_tokens) is not int or max_tokens < 1 or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 600:
        raise OperationalError('Invalid CLI review budget')
    cwd_value, ledger_value = os.environ.get('REVIEW_DEMO_CLI_WORKDIR'), os.environ.get('REVIEW_DEMO_LEDGER')
    if not cwd_value or not ledger_value:
        raise OperationalError('Explicit trusted CLI workdir and local attempt ledger required')
    cwd = checked_path(cwd_value)
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(cwd.iterdir()):
        raise OperationalError('CLI model workdir must be empty and trusted')
    payload = json.dumps(context, ensure_ascii=True)
    if len((prompt + payload).encode('utf-8')) > max_tokens:
        raise OperationalError('Context exceeds conservative input token ceiling')
    args = [str(Path.home() / '.local/bin/claude'), '-p', '--model', 'claude-opus-5-5',
            '--effort', 'medium', '--safe-mode', '--restricted', '--tools', '',
            '--disable-slash-commands', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--no-chrome', '--no-session-persistence', '--system-prompt-snapshot', 'off',
            '--system-prompt', prompt, '--permission-mode', 'plan', '--permission-prompts', 'none',
            '--max-budget-usd', '1', '--output-format', 'json']
    reserve_claude_attempt(ledger_value)
    try:
        code, stdout = run_claude_process(args, cwd, claude_cli_environment(), payload,
                                          min(timeout_seconds, 120))
        if code or len(stdout.encode('utf-8')) > 8 * 1024 * 1024:
            raise OperationalError('Claude CLI failed or response exceeded the local size limit')
        decoded = decode_claude_cli(json.loads(stdout))
        if decoded['tokens']['out'] > max_tokens:
            raise OperationalError('Claude CLI exceeded requested output budget')
        return decoded
    except (subprocess.SubprocessError, OSError, ValueError):
        raise OperationalError('Claude CLI review unfinished; attempt retained, no retry or fallback') from None
