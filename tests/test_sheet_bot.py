"""
Telegram flow for sheet text copied on a phone (Google Lens etc.).
Run:  python3 -m unittest tests.test_sheet_bot -v

The Anthropic client and the Telegram API are both faked: no network.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

_TMP = tempfile.TemporaryDirectory()
os.environ['QUOTATION_DB'] = os.path.join(_TMP.name, 'test.db')
os.environ['QUOTATION_VISION_DIR'] = os.path.join(_TMP.name, 'vision')
os.environ['ADMIN_PASSWORD'] = 'Test-Passw0rd!'
os.environ['APQMS_TELEGRAM_BOT_TOKEN'] = '123:TEST'
os.environ['APQMS_TELEGRAM_WEBHOOK_SECRET'] = 'hook-secret'
os.environ['APQMS_TELEGRAM_ALLOWED_CHATS'] = '111'

import http.server                                       # noqa: E402

from utils import http_utils                             # noqa: E402
for _attr, _name in (('_SESSIONS_FILE', 'sessions.json'), ('_CSRF_FILE', 'csrf.json'),
                     ('_ATTEMPTS_FILE', 'attempts.json')):
    setattr(http_utils, _attr, os.path.join(_TMP.name, _name))

import database                                          # noqa: E402
from server import QuotationHandler                      # noqa: E402
from app.assistant import sheet_bot, telegram            # noqa: E402
from app.vision import extractor, store                  # noqa: E402
from app.vision.prompt import TEXT_SYSTEM_PROMPT         # noqa: E402
from app.vision.schema import SAMPLE_GROUND_TRUTH        # noqa: E402
from test_vision import FakeClient                       # noqa: E402

telegram.ASYNC_WEBHOOK = False

# Roughly what Google Lens gives for the sample sheet in the brief.
LENS_TEXT = """AYPEE IN
Forged Type Chain Pitch 142mm (Heavy Duty)
Link forged from 20MnCr5 Hardened & Tempered
Pin (20MnCr5) 25mm with lock washer both sides
C Attachment (MOC-EN8D) Height 98mm Thickness 10mm
Total Width 471mm
Quantity: 70 mtrs
Rate: Rs. 7600/- per mtr 83601-bent.
GST 18% extra
Rates ex Factory Jalandhar
Delivery: 5-6 weeks from date of order
Payment: 100% on delivery 20 25% advance & balance as used in one month
Note: In yor inquiry attachmet material is 20MnCr5, we have ofered you attachment material EN8D
A106G1Z4"""

MODEL_OUT = dict(SAMPLE_GROUND_TRUTH, needs_review=[
    {'field': 'rate_per_mtr', 'reason': 'both 7600 (printed) and "83601-bent." (handwritten, read as 8360) appear'},
    {'field': 'delivery_weeks', 'reason': 'only "5-6" visible in the text; strikethrough/handwritten 8 not certain'},
])


class SheetFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        db = database.get_db()
        # When run with `unittest discover` the modules share one database,
        # so only add what isn't there yet.
        for contact, name in (('R', 'Krishna Sugar Mills Ltd.'), ('S', 'Shree Ram Sugars')):
            if not db.execute("SELECT 1 FROM customers WHERE company_name=?", (name,)).fetchone():
                db.execute("INSERT INTO customers (contact_person, company_name) VALUES (?,?)", (contact, name))
        db.execute("INSERT INTO items (item_name, unit, rate_per_unit) VALUES ('Roller Chain 25.4', 'Mtr.', 450)")
        db.commit()
        db.close()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuotationHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.orig = (telegram._call, telegram._send_document, sheet_bot._extract)

    @classmethod
    def tearDownClass(cls):
        telegram._call, telegram._send_document, sheet_bot._extract = cls.orig
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.sent = []
        telegram._call = lambda m, p=None, timeout=30: self.sent.append((m, p)) or {'ok': True}
        telegram._send_document = lambda chat, name, content, caption='': self.sent.append(('doc', name, content))
        self.fake = FakeClient(json.dumps(MODEL_OUT))
        sheet_bot._extract = lambda text: extractor.extract_text(text, client=self.fake)

    def say(self, text, chat=111):
        self.sent.clear()
        body = json.dumps({'message': {'chat': {'id': chat}, 'text': text}}).encode()
        req = urllib.request.Request(self.base + '/api/v1/telegram/webhook', data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'X-Telegram-Bot-Api-Secret-Token': 'hook-secret'})
        self.assertEqual(urllib.request.urlopen(req).status, 200)
        return '\n---\n'.join(x[1]['text'] for x in self.sent if x[0] == 'sendMessage')

    def docs(self):
        return [x for x in self.sent if x[0] == 'doc']

    def test_full_text_flow(self):
        out = self.say(LENS_TEXT)
        self.assertIn('padh raha hoon', out)                       # interim message
        self.assertIn('Rate: Rs. 8,360.00 / mtr', out)
        self.assertIn('purani: Rs. 7,600.00', out)
        self.assertIn('83601-bent.', out)                           # doubt shown with the raw text
        self.assertIn('ok <customer naam>', out)
        kw = self.fake.calls[0]
        self.assertEqual(kw['system'][0]['text'], TEXT_SYSTEM_PROMPT)
        self.assertIn('<copied_text>', kw['messages'][0]['content'][0]['text'])
        self.assertNotIn('temperature', kw)

        out = self.say('delivery 8\nrate 8360')                     # user checks the sheet and fixes
        self.assertIn('Update ho gaya', out)
        self.assertNotIn('5-6', out)                                # fixed fields lose their warning

        out = self.say('item chain')                                # normal commands still work
        self.assertIn('Roller Chain 25.4', out)

        out = self.say('ok')                                        # customer not given yet
        self.assertIn('Customer kaun hai', out)
        out = self.say('ok sugar')                                  # ambiguous
        self.assertIn('kai customer', out)
        out = self.say('ok krishna')
        self.assertIn('ban gayi', out)
        self.assertEqual(len(self.docs()), 1)
        self.assertTrue(self.docs()[0][2].startswith(b'%PDF'))

        rows = store.recent()
        row = store.get(rows[0]['id'])
        self.assertEqual(row['source'], 'text')
        self.assertEqual(row['source_text'], LENS_TEXT)
        self.assertEqual(row['status'], store.STATUS_CONFIRMED)
        self.assertEqual(row['confirmed']['delivery_weeks'], 8)
        db = database.get_db()
        it = db.execute("SELECT * FROM quotation_items WHERE quotation_id=?", (row['quotation_id'],)).fetchone()
        q = db.execute("SELECT * FROM quotations WHERE id=?", (row['quotation_id'],)).fetchone()
        c = db.execute("SELECT company_name FROM customers WHERE id=?", (q['customer_id'],)).fetchone()
        db.close()
        self.assertEqual((it['item_name'], it['quantity'], it['rate']),
                         ('Forged Type Chain, Pitch 142mm (Heavy Duty)', 70, 8360))
        self.assertEqual(c['company_name'], 'Krishna Sugar Mills Ltd.')
        self.assertIn('Delivery: 8 weeks', q['terms'])

        out = self.say('ok krishna')                                # nothing pending any more
        self.assertNotIn('ban gayi', out)
        out = self.say('isko proforma bana do')                     # follow-up uses the new quotation
        self.assertIn('Proforma API-', out)

    def test_cancel_and_failure(self):
        self.say(LENS_TEXT)
        out = self.say('cancel')
        self.assertIn('chhod di', out)
        out = self.say('rate 9000')                                 # no pending sheet: normal lookup
        self.assertNotIn('Update ho gaya', out)

        def boom(text):
            raise extractor.ExtractionError('AI service is busy')
        sheet_bot._extract = boom
        out = self.say(LENS_TEXT)
        self.assertIn('padh nahi paya', out)

    def test_detection(self):
        self.assertTrue(sheet_bot.looks_like_sheet(LENS_TEXT))
        self.assertTrue(sheet_bot.looks_like_sheet('sheet\nwhatever'))
        for normal in ('quotation Krishna Sugar\n10 mtr roller chain 25.4 @ 450\nbearing 6205 x 5',
                       'AQ-2026-012 ka proforma bana do', 'summary', 'item chain 25.4'):
            self.assertFalse(sheet_bot.looks_like_sheet(normal), normal)


if __name__ == '__main__':
    unittest.main()
