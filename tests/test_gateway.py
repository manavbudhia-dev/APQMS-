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

ENV_KEYS = ('APQMS_AI_BASE_URL', 'APQMS_AI_API_KEY', 'APQMS_AI_MODEL', 'APQMS_AI_COMPAT',
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
        self.requests = []
        self.responses = []

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        extractor._compat_learned = False

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


if __name__ == '__main__':
    unittest.main()
