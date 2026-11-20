"""
Photo Quote tests. Run:  python3 -m unittest tests.test_vision -v

No network and no API key: the Anthropic client is replaced by a fake that
returns canned model output, so these check our side of the pipeline —
request shape, JSON parsing / retry, validation, needs_review handling,
the review screen's confirmation rules, and quotation + PDF creation.
For a real accuracy check on a photo, use tools/eval_vision.py.
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

_TMP = tempfile.TemporaryDirectory()
os.environ['QUOTATION_DB'] = os.path.join(_TMP.name, 'test.db')
os.environ['QUOTATION_VISION_DIR'] = os.path.join(_TMP.name, 'vision')
os.environ['ADMIN_PASSWORD'] = 'Test-Passw0rd!'

import http.server                                       # noqa: E402

from utils import http_utils                             # noqa: E402
for _attr, _name in (('_SESSIONS_FILE', 'sessions.json'), ('_CSRF_FILE', 'csrf.json'),
                     ('_ATTEMPTS_FILE', 'attempts.json')):
    setattr(http_utils, _attr, os.path.join(_TMP.name, _name))

import database                                          # noqa: E402
from server import QuotationHandler                      # noqa: E402
from app.vision import extractor, store                  # noqa: E402
from app.vision.schema import EXTRACTION_SCHEMA, SAMPLE_GROUND_TRUTH  # noqa: E402
from http_client import Client                           # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None


def png_bytes(size=(60, 40), exif_orientation=None):
    if Image is None:      # 1x1 PNG
        return (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00'
                b'\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00'
                b'\x00\x00IEND\xaeB`\x82')
    img = Image.new('RGB', size, (240, 240, 230))
    buf = io.BytesIO()
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(buf, format='JPEG', exif=exif)
    else:
        img.save(buf, format='PNG')
    return buf.getvalue()


class FakeClient:
    """Stands in for anthropic.Anthropic(); returns queued texts in order."""

    def __init__(self, *texts, stop_reason='end_turn'):
        self.texts = list(texts)
        self.calls = []
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type='thinking', thinking=''),
                                        SimpleNamespace(type='text', text=text)],
                               stop_reason=self.stop_reason, model=kwargs['model'],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=300))


class Extraction(unittest.TestCase):
    def test_request_shape(self):
        fake = FakeClient(json.dumps(SAMPLE_GROUND_TRUTH))
        res = extractor.extract(png_bytes(), client=fake)
        self.assertEqual(res.data, SAMPLE_GROUND_TRUTH)
        self.assertEqual(res.attempts, 1)
        kw = fake.calls[0]
        self.assertEqual(kw['model'], 'claude-opus-5-5')
        self.assertNotIn('temperature', kw)                 # rejected by current Opus models
        self.assertEqual(kw['output_config']['format'], {'type': 'json_schema', 'schema': EXTRACTION_SCHEMA})
        self.assertEqual(kw['fallbacks'], 'default')
        content = kw['messages'][0]['content']
        self.assertEqual(content[0]['type'], 'image')
        self.assertEqual(content[0]['source']['type'], 'base64')
        self.assertEqual(content[1]['type'], 'text')
        self.assertIn('strikethrough', kw['system'][0]['text'])

    def test_code_fence_is_stripped(self):
        fake = FakeClient('```json\n' + json.dumps(SAMPLE_GROUND_TRUTH) + '\n```')
        self.assertEqual(extractor.extract(png_bytes(), client=fake).data['rate_per_mtr'], 8360)

    def test_retry_once_on_invalid_json(self):
        fake = FakeClient('{"rate_per_mtr": 83', json.dumps(SAMPLE_GROUND_TRUTH))
        res = extractor.extract(png_bytes(), client=fake)
        self.assertEqual(res.attempts, 2)
        retry_msgs = fake.calls[1]['messages']
        self.assertEqual(retry_msgs[1]['role'], 'assistant')
        self.assertIn('not valid JSON', retry_msgs[2]['content'])

    def test_gives_up_after_second_invalid(self):
        fake = FakeClient('nope', 'still nope')
        with self.assertRaises(extractor.ExtractionError) as cm:
            extractor.extract(png_bytes(), client=fake)
        self.assertEqual(cm.exception.raw_text, 'still nope')
        self.assertEqual(len(fake.calls), 2)

    def test_refusal(self):
        fake = FakeClient('', stop_reason='refusal')
        with self.assertRaises(extractor.ExtractionError):
            extractor.extract(png_bytes(), client=fake)

    def test_validation_coerces_and_flags(self):
        raw = dict(SAMPLE_GROUND_TRUTH, rate_per_mtr='8,360/-', delivery_weeks=None, gst_percent=17,
                   needs_review=[{'field': 'total_width_mm', 'reason': '471 or 474'}])
        data, problems = extractor.validate(raw)
        self.assertEqual(data['rate_per_mtr'], 8360)
        self.assertEqual(problems, ['delivery_weeks'])
        fields = [r['field'] for r in data['needs_review']]
        for f in ('rate_per_mtr', 'delivery_weeks', 'gst_percent', 'total_width_mm'):
            self.assertIn(f, fields)

    def test_validation_never_guesses(self):
        data, problems = extractor.validate({'rate_per_mtr': '83?0', 'quantity_mtrs': True})
        self.assertIsNone(data['rate_per_mtr'])
        self.assertIsNone(data['quantity_mtrs'])
        self.assertIn('rate_per_mtr', problems)

    @unittest.skipIf(Image is None, 'Pillow not installed')
    def test_preprocess_rotates_from_exif(self):
        out, media_type, changed = extractor.preprocess(png_bytes((200, 100), exif_orientation=6))
        self.assertTrue(changed)
        self.assertEqual(media_type, 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(out)).size, (100, 200))

    def test_compare_against_ground_truth(self):
        rows = extractor.compare(dict(SAMPLE_GROUND_TRUTH, delivery_weeks=6), SAMPLE_GROUND_TRUTH)
        bad = [r[0] for r in rows if not r[3]]
        self.assertEqual(bad, ['delivery_weeks'])


class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        db = database.get_db()
        db.execute("INSERT INTO customers (contact_person, company_name) VALUES ('R', 'Krishna Sugar Mills')")
        db.execute("INSERT INTO items (item_name, unit, rate_per_unit) VALUES ('Forged Chain 142mm', 'Mtr.', 5000)")
        db.commit()
        db.close()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuotationHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.c = Client(f'http://127.0.0.1:{cls.server.server_address[1]}')
        assert cls.c.login('admin', os.environ['ADMIN_PASSWORD'])[0] == 302
        cls.orig_extract = QuotationHandler._vision_extract
        cls.orig_available = extractor.available
        extractor.available = lambda: (True, '')

    @classmethod
    def tearDownClass(cls):
        QuotationHandler._vision_extract = cls.orig_extract
        extractor.available = cls.orig_available
        cls.server.shutdown()
        cls.server.server_close()

    def upload(self, model_json):
        fake = FakeClient(json.dumps(model_json))
        QuotationHandler._vision_extract = lambda self, data: extractor.extract(data, client=fake)
        s, h, html = self.c.get('/photo-quote')
        self.assertEqual(s, 200)
        s, h, b = self.c.post_multipart('/photo-quote/upload', {'csrf_token': self.c.csrf_from(html)},
                                        {'photo': ('sheet.png', 'image/png', png_bytes())})
        self.assertEqual(s, 302, b)
        rid = int(h['Location'].rsplit('/', 1)[1])
        return rid

    def review_form(self, rid):
        s, h, html = self.c.get(f'/photo-quote/{rid}')
        self.assertEqual(s, 200)
        return html

    def base_pairs(self, html, overrides=None, ticks=True):
        data = dict(SAMPLE_GROUND_TRUTH)
        flat = {'product': data['product'], 'link_material': data['link_material'], 'pin': data['pin'],
                'attachment_material': 'EN8D', 'attachment_height_mm': '98', 'attachment_thickness_mm': '10',
                'total_width_mm': '471', 'quantity_mtrs': '70', 'rate_per_mtr': '8360',
                'original_rate_per_mtr': '7600', 'gst_percent': '18', 'price_basis': data['price_basis'],
                'delivery_weeks': '8', 'payment_terms': data['payment_terms'], 'notes': data['notes'],
                'customer_id': '1'}
        flat.update(overrides or {})
        pairs = [('csrf_token', self.c.csrf_from(html))] + list(flat.items())
        if ticks:
            pairs += [('confirm_rate_per_mtr', '1'), ('confirm_quantity_mtrs', '1'),
                      ('confirm_delivery_weeks', '1')]
        return pairs

    def test_full_flow(self):
        flagged = dict(SAMPLE_GROUND_TRUTH,
                       needs_review=[{'field': 'rate_per_mtr', 'reason': 'last digit: 8360 or 8368'}])
        rid = self.upload(flagged)
        row = store.get(rid)
        self.assertEqual(row['status'], store.STATUS_EXTRACTED)
        self.assertTrue(store.read_image(row).startswith(b'\x89PNG'))     # original kept for audit
        html = self.review_form(rid)
        self.assertIn('8360 or 8368', html)                               # needs_review highlighted
        self.assertEqual(html.count('name="confirm_'), 3)                 # rate, qty, delivery
        self.assertIn('value="8360"', html)

        # Without the three ticks nothing is created.
        s, h, b = self.c.post_form(f'/photo-quote/{rid}/confirm', self.base_pairs(html, ticks=False))
        self.assertEqual(s, 400)
        self.assertIn('tick', b.decode())
        self.assertIsNone(store.get(rid)['quotation_id'])

        # User corrects the rate and confirms.
        html = self.review_form(rid)
        s, h, b = self.c.post_form(f'/photo-quote/{rid}/confirm',
                                   self.base_pairs(html, {'rate_per_mtr': '8368'}))
        self.assertEqual(s, 302, b)
        qid = int(h['Location'].rsplit('/', 1)[1])
        row = store.get(rid)
        self.assertEqual(row['status'], store.STATUS_CONFIRMED)
        self.assertEqual(row['confirmed']['rate_per_mtr'], 8368)
        self.assertEqual(row['extracted']['rate_per_mtr'], 8360)          # audit keeps both

        db = database.get_db()
        q = db.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        it = db.execute("SELECT * FROM quotation_items WHERE quotation_id=?", (qid,)).fetchone()
        db.close()
        self.assertEqual(it['item_name'], 'Forged Type Chain, Pitch 142mm (Heavy Duty)')   # not the catalog name
        self.assertEqual((it['quantity'], it['unit'], it['rate']), (70, 'Mtr.', 8368))
        self.assertIn('Attachment: MOC EN8D, Height 98mm, Thickness 10mm', it['specification'])
        self.assertIn('Delivery: 8 weeks', q['terms'])
        self.assertIn('25% advance', q['terms'])
        self.assertIn('Ex Factory Jalandhar', q['terms'])
        self.assertIn('EN8D', q['customer_notes'])
        self.assertIn('revised from 7600 to 8368', q['notes'])

        s, h, pdf = self.c.get(f'/quotations/{qid}/pdf')
        self.assertEqual(s, 200)
        self.assertTrue(pdf.startswith(b'%PDF'))

        # Second submit doesn't make a second quotation.
        s, h, b = self.c.post_form(f'/photo-quote/{rid}/confirm', self.base_pairs(self.review_form(rid)))
        self.assertEqual(h['Location'], f'/quotations/{qid}')

    def test_failed_extraction_allows_manual_entry(self):
        rid = self.upload({'oops': True})            # validates, but required fields missing
        html = self.review_form(rid)
        self.assertIn('missing', html)
        s, h, b = self.c.post_form(f'/photo-quote/{rid}/confirm',
                                   self.base_pairs(html, {'delivery_weeks': 'eight'}))
        self.assertEqual(s, 400)
        self.assertIn('number nahi hai', b.decode())

    def test_image_route_requires_login(self):
        rid = self.upload(SAMPLE_GROUND_TRUTH)
        anon = Client(self.c.base)
        s, h, b = anon.get(f'/photo-quote/{rid}/image')
        self.assertEqual(s, 302)
        s, h, b = self.c.get(f'/photo-quote/{rid}/image')
        self.assertEqual(s, 200)


if __name__ == '__main__':
    unittest.main()
