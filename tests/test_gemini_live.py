"""Offline Gemini API adapter tests, constructed usage is not live acceptance."""
import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from poc import model_adapter as adapter

ENV = {'MODEL_PROVIDER': 'gemini', 'MODEL_ID': 'test-pinned-001',
       'MODEL_ENDPOINT': 'https://generativelanguage.googleapis.com/v1beta/models/test-pinned-001:generateContent',
       'MODEL_API_KEY': 'synthetic-unit-key', 'MODEL_INPUT_USD_PER_MILLION': '2',
       'MODEL_OUTPUT_USD_PER_MILLION': '10', 'MODEL_COST_BASIS': 'Synthetic dated test pricing; not real model rates',
       'MODEL_MAX_OUTPUT_TOKENS': '2048'}
RAW = {'modelVersion': 'test-actual-001', 'candidates': [
    {'finishReason': 'STOP', 'content': {'parts': [{'text': '[]'}]}}],
    'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 20,
                      'thoughtsTokenCount': 30, 'totalTokenCount': 150}}


class GeminiLiveTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = copy.deepcopy(RAW)
        self.failure = None
        test = self
        class Endpoint:
            def open(self, request, timeout):
                test.requests.append((request, timeout))
                if test.failure:
                    raise test.failure
                return io.BytesIO(json.dumps(test.response).encode())
        self.environment = patch.dict(adapter.os.environ, ENV, clear=True)
        self.transport = patch.object(adapter.urllib.request, 'build_opener', return_value=Endpoint())
        self.environment.start()
        self.transport.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.transport.stop)

    def call(self, **kwargs):
        return adapter.call_model('Trusted instructions', {'source': 'package main'},
                                  timeout_seconds=2, max_tokens=10000, allow_live=True, **kwargs)

    def test_single_api_call_uses_actual_model_usage_including_thinking_and_explicit_estimate(self):
        result = self.call()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['model'], 'gemini/test-actual-001')
        self.assertEqual(result['tokens'], {'in': 100, 'out': 50})
        self.assertAlmostEqual(result['cost_usd'], 0.0007)
        self.assertIn('estimate', result['receipt_basis'])
        self.assertIn('not billed', result['receipt_basis'])
        self.assertIn(ENV['MODEL_COST_BASIS'], result['receipt_basis'])
        self.assertEqual(result['text'], '[]')

    def test_key_only_in_header_and_no_tools_with_bounded_supported_output(self):
        self.call()
        request, timeout = self.requests[0]
        body = json.loads(request.data)
        self.assertEqual(request.get_header('X-goog-api-key'), ENV['MODEL_API_KEY'])
        self.assertNotIn(ENV['MODEL_API_KEY'], request.full_url + request.data.decode())
        self.assertEqual(timeout, 2)
        self.assertEqual(body['generationConfig']['maxOutputTokens'], 2048)
        self.assertNotIn('candidateCount', body['generationConfig'])
        self.assertNotIn('tools', body)
        self.assertEqual(set(body), {'systemInstruction', 'contents', 'generationConfig'})

    def test_missing_model_pricing_basis_key_or_invalid_rates_fail_before_network(self):
        variants = [{key: ''} for key in ('MODEL_ID', 'MODEL_ENDPOINT', 'MODEL_API_KEY',
                    'MODEL_INPUT_USD_PER_MILLION', 'MODEL_OUTPUT_USD_PER_MILLION', 'MODEL_COST_BASIS')]
        variants += [{'MODEL_INPUT_USD_PER_MILLION': rate} for rate in ('nan', 'inf', '-1', 'oops')]
        variants += [{'MODEL_MAX_OUTPUT_TOKENS': value} for value in ('0', '-1', 'nan', '1.5')]
        for mutation in variants:
            with self.subTest(mutation=mutation), patch.dict(adapter.os.environ, mutation):
                with self.assertRaises(adapter.OperationalError):
                    self.call()
        self.assertEqual(self.requests, [])

    def test_http_failure_does_not_retry_or_echo_key_url_or_response_body(self):
        self.failure = urllib.error.HTTPError(ENV['MODEL_ENDPOINT'] + '?key=synthetic-unit-key',
            429, 'synthetic-unit-key sensitive response', {}, None)
        with self.assertRaises(adapter.OperationalError) as raised:
            self.call()
        self.assertNotIn('synthetic-unit-key', str(raised.exception))
        self.assertNotIn('sensitive', str(raised.exception))
        self.assertEqual(len(self.requests), 1)

    def test_unfinished_missing_or_inconsistent_usage_cannot_produce_estimate(self):
        for mutation in ('blocked', 'missing', 'bad-total', 'negative-thinking', 'tool-use', 'wrong-thinking'):
            self.response = copy.deepcopy(RAW)
            if mutation == 'blocked': self.response['candidates'][0]['finishReason'] = 'MAX_TOKENS'
            if mutation == 'missing': del self.response['usageMetadata']
            if mutation == 'bad-total': self.response['usageMetadata']['totalTokenCount'] = 119
            if mutation == 'negative-thinking': self.response['usageMetadata']['thoughtsTokenCount'] = -1
            if mutation == 'tool-use': self.response['usageMetadata']['toolUsePromptTokenCount'] = 3
            if mutation == 'wrong-thinking': self.response['usageMetadata']['thoughtsTokenCount'] = 31
            with self.subTest(mutation=mutation), self.assertRaises(adapter.OperationalError):
                self.call()

    def test_measured_total_over_requested_budget_is_operational_failure(self):
        self.response['usageMetadata'].update(promptTokenCount=10000, totalTokenCount=10050)
        with self.assertRaisesRegex(adapter.OperationalError, 'budget'):
            self.call()

    def test_cached_input_remains_explicit_uncached_list_price_estimate(self):
        self.response['usageMetadata']['cachedContentTokenCount'] = 80
        result = self.call()
        self.assertAlmostEqual(result['cost_usd'], 0.0007)
        self.assertIn('uncached', result['receipt_basis'])

    def test_gemini_three_text_thought_signatures_are_metadata_not_finding_text(self):
        self.response['candidates'][0]['content']['parts'] = [
            {'text': '[]', 'thoughtSignature': 'opaque-encrypted-signature'},
            {'thoughtSignature': 'opaque-trailing-signature'}]
        result = self.call()
        self.assertEqual(result['text'], '[]')
        self.assertNotIn('opaque', result['text'])

    def test_nontext_parts_and_multiple_candidates_remain_unfinished(self):
        for part in ({'functionCall': {'name': 'unrequested-tool'}}, {'text': '[]', 'thought': True},
                     {'text': '[]', 'thoughtSignature': 1}):
            self.response = copy.deepcopy(RAW)
            self.response['candidates'][0]['content']['parts'] = [part]
            with self.subTest(part=part), self.assertRaises(adapter.OperationalError):
                self.call()
        self.response = copy.deepcopy(RAW)
        self.response['candidates'] *= 2
        with self.assertRaises(adapter.OperationalError):
            self.call()

    def test_zero_estimate_is_only_possible_from_explicit_trusted_zero_rates(self):
        with patch.dict(adapter.os.environ, {'MODEL_INPUT_USD_PER_MILLION': '0',
                    'MODEL_OUTPUT_USD_PER_MILLION': '0', 'MODEL_COST_BASIS': 'Synthetic explicit free-tier estimate'}):
            result = self.call()
        self.assertEqual(result['cost_usd'], 0)
        self.assertIn('Synthetic explicit free-tier estimate', result['receipt_basis'])

    def test_disabled_mode_never_reaches_provider_even_with_complete_configuration(self):
        with self.assertRaisesRegex(adapter.OperationalError, 'disabled'):
            adapter.call_model('prompt', {}, timeout_seconds=2, max_tokens=10000)
        self.assertEqual(self.requests, [])

    def test_official_endpoint_model_matching_and_redirects_fail_closed(self):
        for endpoint in ('https://example.invalid/model', ENV['MODEL_ENDPOINT'] + '?key=synthetic-unit-key',
                         ENV['MODEL_ENDPOINT'].replace('test-pinned-001', 'other-model')):
            with self.subTest(endpoint=endpoint), patch.dict(adapter.os.environ, {'MODEL_ENDPOINT': endpoint}):
                with self.assertRaises(adapter.OperationalError):
                    self.call()
        self.assertEqual(self.requests, [])
        with self.assertRaises(adapter.OperationalError):
            adapter._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://example.invalid/')
