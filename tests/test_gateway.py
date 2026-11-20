"""
Third-party gateway support (APQMS_AI_BASE_URL / _API_KEY / _MODEL / _COMPAT).
Run:  python3 -m unittest tests.test_gateway -v

Uses the real Anthropic SDK with a mock HTTP transport, so it checks the
exact requests a gateway receives, without any network.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

from app.vision import extractor                         # noqa: E402
from app.vision.schema import SAMPLE_GROUND_TRUTH        # noqa: E402

try:
    import anthropic
    import httpx2
except ImportError:
    anthropic = httpx2 = None

ENV_KEYS = ('APQMS_AI_BASE_URL', 'APQMS_AI_API_KEY', 'APQMS_AI_MODEL', 'APQMS_AI_COMPAT', 'APQMS_AI_PROTOCOL',
            'ANTHROPIC_API_KEY', 'APQMS_VISION_MODEL')


def ok_message(text, model='gw-claude'):
    return {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': model,
            'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 10, 'output_tokens': 20}}


@unittest.skipIf(anthropic is None, 'anthropic SDK not installed')
class Gateway(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({'APQMS_AI_BASE_URL': 'https://gw.example.com/v1/', 'APQMS_AI_API_KEY': 'gw-key-123',
                           'APQMS_AI_MODEL': 'claude-opus-4-5'})
        extractor._compat_learned = False
        extractor._openai_learned = False
        self.requests = []
        self.responses = []

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        extractor._compat_learned = False
        extractor._openai_learned = False

    def client(self):
        def handler(request):
            body = json.loads(request.content or b'{}')
            self.requests.append({'url': str(request.url), 'headers': dict(request.headers), 'body': body})
            status, payload = self.responses.pop(0)
            return httpx2.Response(status, json=payload)
        c = extractor.make_client()
        # same client settings, mock transport instead of the network
        return c.with_options(http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
                              max_retries=0)

    def test_settings_and_headers(self):
        st = extractor.ai_settings()
        self.assertEqual(st['base_url'], 'https://gw.example.com')        # trailing /v1/ removed
        self.assertTrue(extractor.available()[0])
        self.responses = [(200, ok_message(json.dumps(SAMPLE_GROUND_TRUTH)))]
        res = extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=self.client())
        r = self.requests[0]
        self.assertEqual(r['url'], 'https://gw.example.com/v1/messages')
        self.assertEqual(r['headers']['x-api-key'], 'gw-key-123')
        self.assertEqual(r['headers']['authorization'], 'Bearer gw-key-123')
        self.assertEqual(r['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(r['body']['model'], 'claude-opus-4-5')
        self.assertEqual(res.data['rate_per_mtr'], 8360)
        self.assertEqual(res.model, 'gw-claude')

    def test_auto_switches_to_basic_request(self):
        self.responses = [
            (400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Unknown field: fallbacks'}}),
            (200, ok_message('```json\n' + json.dumps(SAMPLE_GROUND_TRUTH) + '\n```')),
            (200, ok_message(json.dumps(SAMPLE_GROUND_TRUTH))),
        ]
        c = self.client()
        res = extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=c)
        self.assertEqual(res.data['delivery_weeks'], 8)
        full, basic = self.requests[0]['body'], self.requests[1]['body']
        self.assertIn('fallbacks', full)
        self.assertIn('output_config', full)
        for field in ('fallbacks', 'output_config', 'betas'):
            self.assertNotIn(field, basic)
        self.assertIsInstance(basic['system'], str)
        self.assertIn('"rate_per_mtr"', basic['system'])                  # schema moved into the prompt
        self.assertNotIn('anthropic-beta', self.requests[1]['headers'])
        # Learned: the next extraction goes straight to the basic request.
        extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=c)
        self.assertEqual(len(self.requests), 3)
        self.assertNotIn('fallbacks', self.requests[2]['body'])

    def test_compat_on_and_off(self):
        os.environ['APQMS_AI_COMPAT'] = 'on'
        self.responses = [(200, ok_message(json.dumps(SAMPLE_GROUND_TRUTH)))]
        extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=self.client())
        self.assertNotIn('output_config', self.requests[0]['body'])

        os.environ['APQMS_AI_COMPAT'] = 'off'
        self.responses = [(400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                             'message': 'Unknown field: fallbacks'}})]
        with self.assertRaises(extractor.ExtractionError) as cm:
            extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=self.client())
        self.assertIn('Unknown field', cm.exception.message)

    def test_readable_errors(self):
        self.responses = [(404, {'type': 'error', 'error': {'type': 'not_found_error', 'message': 'model not found'}})]
        with self.assertRaises(extractor.ExtractionError) as cm:
            extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=self.client())
        self.assertIn('base URL ya model', cm.exception.message)
        self.assertIn('claude-opus-4-5', cm.exception.message)
        self.responses = [(401, {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'bad key'}})]
        with self.assertRaises(extractor.ExtractionError) as cm:
            extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=self.client())
        self.assertIn('key galat', cm.exception.message)

    def test_direct_anthropic_unchanged(self):
        os.environ.pop('APQMS_AI_BASE_URL')
        os.environ.pop('APQMS_AI_MODEL')
        st = extractor.ai_settings()
        self.assertEqual((st['base_url'], st['model']), ('', 'claude-opus-5-5'))
        c = extractor.make_client()
        self.assertEqual(str(c.base_url).rstrip('/'), 'https://api.anthropic.com')


class _FakeOpenAIGateway:
    """Stands in for OpenAICompatClient.post: records requests, returns canned JSON."""

    def __init__(self, test):
        self.test = test

    def __call__(self, client_self, path, body):
        self.test.requests.append({'url': client_self.base_url + path, 'body': body,
                                   'key': client_self.api_key})
        status, payload = self.test.responses.pop(0)
        if status != 200:
            raise extractor.GatewayHTTPError(status, payload)
        return payload


def oai_message(text, model='anthropic/claude-haiku-5.5', finish='stop'):
    return {'model': model, 'choices': [{'message': {'role': 'assistant', 'content': text},
                                         'finish_reason': finish}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 22}}


@unittest.skipIf(anthropic is None, 'anthropic SDK not installed')
class OpenAIFormatGateway(Gateway):
    def setUp(self):
        super().setUp()
        self._orig_post = extractor.OpenAICompatClient.post
        fake = _FakeOpenAIGateway(self)
        extractor.OpenAICompatClient.post = lambda client_self, path, body: fake(client_self, path, body)

    def tearDown(self):
        extractor.OpenAICompatClient.post = self._orig_post
        super().tearDown()

    # inherited Anthropic-format tests don't apply when protocol is forced
    test_settings_and_headers = test_auto_switches_to_basic_request = None
    test_compat_on_and_off = test_readable_errors = test_direct_anthropic_unchanged = None

    def test_forced_openai(self):
        os.environ['APQMS_AI_PROTOCOL'] = 'openai'
        c = extractor.make_client()
        self.assertIsInstance(c, extractor.OpenAICompatClient)
        self.responses = [(200, oai_message('```json\n' + json.dumps(SAMPLE_GROUND_TRUTH) + '\n```'))]
        res = extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=c)
        r = self.requests[0]
        self.assertEqual(r['url'], 'https://gw.example.com/v1/chat/completions')
        self.assertEqual(r['key'], 'gw-key-123')
        self.assertEqual(r['body']['messages'][0]['role'], 'system')
        self.assertIn('"rate_per_mtr"', r['body']['messages'][0]['content'])
        self.assertIsInstance(r['body']['messages'][1]['content'], str)
        self.assertEqual(res.data['rate_per_mtr'], 8360)
        self.assertEqual(res.model, 'anthropic/claude-haiku-5.5')
        self.assertEqual(res.usage['output_tokens'], 22)

    def test_image_and_retry(self):
        os.environ['APQMS_AI_PROTOCOL'] = 'openai'
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 50
        self.responses = [(200, oai_message('not json')), (200, oai_message(json.dumps(SAMPLE_GROUND_TRUTH)))]
        extractor.extract(png, client=extractor.make_client(), do_preprocess=False)
        parts = self.requests[0]['body']['messages'][1]['content']
        self.assertEqual(parts[0]['type'], 'image_url')
        self.assertTrue(parts[0]['image_url']['url'].startswith('data:image/png;base64,'))
        retry = self.requests[1]['body']['messages']
        self.assertEqual([m['role'] for m in retry], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(retry[2]['content'], 'not json')

    def test_auto_switch_on_404(self):
        def handler(request):
            return httpx2.Response(404, json={'message': 'Route POST:/v1/messages not found',
                                              'error': 'Not Found', 'statusCode': 404})
        c = extractor.make_client().with_options(
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0)
        self.responses = [(200, oai_message(json.dumps(SAMPLE_GROUND_TRUTH)))]
        res = extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=c)
        self.assertEqual(res.data['delivery_weeks'], 8)
        self.assertTrue(extractor._use_openai())
        self.assertIsInstance(extractor.make_client(), extractor.OpenAICompatClient)

    def test_openai_errors(self):
        os.environ['APQMS_AI_PROTOCOL'] = 'openai'
        self.responses = [(401, 'Missing or invalid API key')]
        with self.assertRaises(extractor.ExtractionError) as cm:
            extractor.extract_text(extractor.SAMPLE_SHEET_TEXT, client=extractor.make_client())
        self.assertIn('key galat', cm.exception.message)

    def test_direct_never_openai(self):
        os.environ.pop('APQMS_AI_BASE_URL')
        os.environ['APQMS_AI_PROTOCOL'] = 'openai'
        self.assertEqual(extractor.ai_settings()['protocol'], 'anthropic')


if __name__ == '__main__':
    unittest.main()
