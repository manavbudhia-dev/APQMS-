"""
Assistant API / bot / CLI tests. Run:  python3 -m unittest tests.test_assistant -v

Uses a throwaway database and a real server on a random port, so it never
touches quotation.db. Telegram calls are stubbed (no network).
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

_TMP = tempfile.TemporaryDirectory()
os.environ['QUOTATION_DB'] = os.path.join(_TMP.name, 'test.db')
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
from app.assistant import bot, core, parser, telegram, tokens   # noqa: E402
import apqms_cli                                         # noqa: E402

telegram.ASYNC_WEBHOOK = False      # process webhook updates inline so tests can assert on them


def seed():
    db = database.get_db()
    try:
        db.executemany("INSERT INTO customers (contact_person, company_name, gstin) VALUES (?,?,?)", [
            ('Ramesh', 'Krishna Sugar Mills Ltd.', '09AAACK1234F1Z5'),
            ('Suresh', 'Shree Ram Sugars Pvt Ltd', ''),
            ('Mahesh', 'Balaji Engineering Works', ''),
        ])
        db.executemany("INSERT INTO items (item_name, description, unit, rate_per_unit) VALUES (?,?,?,?)", [
            ('Roller Chain 25.4 mm pitch', 'Simplex roller chain', 'Mtr.', 450),
            ('Roller Chain 31.75 mm pitch', 'Simplex roller chain', 'Mtr.', 620),
            ('Ball Bearing 6205', 'Deep groove', 'Nos.', 250),
            ('Sprocket 12T', 'For 25.4 chain', 'Pcs.', 0),
        ])
        db.commit()
    finally:
        db.close()


class Assistant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        seed()
        cls.admin = core.user_by_username('admin')
        _, cls.token = tokens.create_token(cls.admin['id'], 'test')
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuotationHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def api(self, method, path, body=None, token=None, headers=None, raw=False):
        h = {'Content-Type': 'application/json'}
        if token is not False:
            h['Authorization'] = f'Bearer {token or self.token}'
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            r = e
        data = r.read()
        return r.status, (data if raw else json.loads(data.decode()))

    # ── parser ──
    def test_parser_item_lines(self):
        it = parser.parse_item('10 mtr roller chain 25.4 @ 450')
        self.assertEqual((it['item'], it['quantity'], it['unit'], it['rate']),
                         ('roller chain 25.4', 10, 'Mtr.', 450))
        it = parser.parse_item('bearing 6205 x 5 rate 1,250')
        self.assertEqual((it['item'], it['quantity'], it['rate']), ('bearing 6205', 5, 1250))
        it = parser.parse_item('grease 5 kg')
        self.assertEqual((it['item'], it['quantity'], it['unit'], it['rate']), ('grease', 5, 'Kg.', None))

    def test_subject_without_colon(self):
        i = parser.parse('quotation Krishna Sugar\n10 mtr roller chain 25.4 @ 450\nSubject chain and spares')
        self.assertEqual(i['args']['subject'], 'chain and spares')
        self.assertEqual(len(i['args']['items']), 1)

    def test_customer_suggestion_yes(self):
        ck = 'test:suggest'
        r = bot.handle('quotation balaji engg works\n5 nos bearing 6205 @ 250\nsubject spares', self.admin,
                       chat_key=ck, attach_pdf=False)
        if r.ok:                                  # matched directly: nothing to test here
            return
        self.assertIn('Balaji Engineering Works', r.text)
        r = bot.handle('Yes', self.admin, chat_key=ck, attach_pdf=False)
        self.assertTrue(r.ok, r.text)
        self.assertIn('Balaji Engineering Works', r.text)
        self.assertIn('spares', r.text)
        r = bot.handle('Yes', self.admin, chat_key=ck, attach_pdf=False)   # pending cleared
        self.assertFalse(r.ok)

    def test_customer_suggestion_pick_and_cancel(self):
        ck = 'test:suggest2'
        tokens.set_pending(f'cust:{ck}', {'kind': 'quotation', 'suggestions': ['Shree Ram Sugars Pvt Ltd',
                                                                              'Krishna Sugar Mills Ltd.'],
                                          'args': {'customer': 'sugar', 'items': [
                                              {'item': 'bearing 6205', 'quantity': 2, 'unit': 'Nos.', 'rate': 250}]}})
        r = bot.handle('haan', self.admin, chat_key=ck, attach_pdf=False)
        self.assertIn('Number likhein', r.text)
        r = bot.handle('2', self.admin, chat_key=ck, attach_pdf=False)
        self.assertTrue(r.ok, r.text)
        self.assertIn('Krishna Sugar Mills', r.text)
        tokens.set_pending(f'cust:{ck}', {'kind': 'quotation', 'suggestions': ['Shree Ram Sugars Pvt Ltd'],
                                          'args': {'customer': 'x', 'items': []}})
        r = bot.handle('no', self.admin, chat_key=ck, attach_pdf=False)
        self.assertIn('chhod diya', r.text)
        self.assertIsNone(tokens.get_pending(f'cust:{ck}'))

    # ── API ──
    def test_auth_required(self):
        s, b = self.api('GET', '/api/v1/items', token=False)
        self.assertEqual(s, 401)
        s, b = self.api('GET', '/api/v1/items', token='apqms_wrong')
        self.assertEqual(s, 401)
        s, b = self.api('GET', '/api/v1/health', token=False)
        self.assertEqual((s, b['ok']), (200, True))
        s, b = self.api('GET', '/api/v1/openapi.json', token=False)
        self.assertIn('/api/v1/chat', b['paths'])

    def test_create_quotation_json_and_convert(self):
        s, b = self.api('POST', '/api/v1/quotations', {
            'customer': 'krishna sugar', 'subject': 'Spares',
            'items': [{'item': 'roller chain 25.4', 'quantity': 10},
                      {'item': 'bearing 6205', 'quantity': 4, 'rate': 240},
                      {'item': 'Custom guard plate', 'quantity': 2, 'rate': 1000, 'unit': 'Pcs.'}]})
        self.assertEqual(s, 201, b)
        d = b['document']
        self.assertTrue(d['number'].startswith('AQ-'))
        self.assertEqual(d['customer']['company_name'], 'Krishna Sugar Mills Ltd.')
        self.assertEqual([i['item_name'] for i in d['items']],
                         ['Roller Chain 25.4 mm pitch', 'Ball Bearing 6205', 'Custom guard plate'])
        self.assertEqual(d['items'][0]['rate'], 450)              # catalog rate
        self.assertEqual(d['totals']['subtotal'], 10 * 450 + 4 * 240 + 2000)

        s, pdf = self.api('GET', f"/api/v1/quotations/{d['number']}/pdf", raw=True)
        self.assertEqual(s, 200)
        self.assertTrue(pdf.startswith(b'%PDF'))

        s, b = self.api('POST', f"/api/v1/quotations/{d['number']}/proforma",
                        {'order_no': 'PO-77', 'mark_confirmed': True})
        self.assertEqual(s, 201, b)
        pi = b['document']
        self.assertTrue(pi['number'].startswith('API-'))
        self.assertEqual(pi['order_no'], 'PO-77')
        self.assertEqual(pi['totals'], d['totals'])
        s, b = self.api('GET', f"/api/v1/quotations/{d['id']}")
        self.assertEqual(b['document']['status'], 'Confirmed')

    def test_validation_errors(self):
        s, b = self.api('POST', '/api/v1/quotations', {'customer': 'sugar', 'items': [{'item': 'x', 'quantity': 1, 'rate': 1}]})
        self.assertEqual(s, 404)                                  # ambiguous: two "sugar" customers
        self.assertEqual(b['error'], 'customer_not_found')
        self.assertTrue(b['detail']['suggestions'])
        s, b = self.api('POST', '/api/v1/proformas', {'customer': 'Balaji', 'items': [{'item': 'sprocket 12T', 'quantity': 3}]})
        self.assertEqual(s, 422)                                  # catalog rate is 0 and none given
        s, b = self.api('POST', '/api/v1/proformas', {'customer': 'Balaji', 'preview': True,
                                                      'items': [{'item': 'sprocket 12T', 'quantity': 3, 'rate': 99}]})
        self.assertEqual(s, 200)
        self.assertTrue(b['document']['preview'])
        self.assertIsNone(b['document']['number'])

    def test_chat_endpoint_flow(self):
        s, b = self.api('POST', '/api/v1/chat', {
            'conversation_id': 'phone',
            'message': 'quotation Shree Ram Sugars\n10 mtr roller chain 31.75 @ 600\nbearing 6205 x 2'})
        self.assertEqual(s, 200, b)
        self.assertIn('ban gayi', b['reply'])
        qno = b['document']['number']
        self.assertTrue(b['pdf_url'].endswith('/pdf'))
        s, b = self.api('POST', '/api/v1/chat', {'conversation_id': 'phone',
                                                 'message': 'isko proforma bana do',
                                                 'include_pdf_base64': True})
        self.assertEqual(s, 200, b)
        self.assertIn(qno, b['reply'])
        self.assertTrue(b['pdf_base64'])
        s, b = self.api('POST', '/api/v1/chat', {'conversation_id': 'phone', 'message': 'status sent kar do isko'})
        self.assertEqual(b['document']['status'], 'Sent')
        s, b = self.api('POST', '/api/v1/chat', {'message': 'quotations'})
        self.assertIn(qno, b['reply'])
        s, b = self.api('POST', '/api/v1/chat', {'message': 'blah blah'})
        self.assertEqual(s, 422)

    def test_html_routes_still_protected(self):
        req = urllib.request.Request(self.base + '/settings/api-tokens')
        opener = urllib.request.build_opener(type('NR', (urllib.request.HTTPRedirectHandler,),
                                                  {'redirect_request': lambda *a, **k: None}))
        try:
            opener.open(req)
            self.fail('expected redirect')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            self.assertIn('/login', e.headers['Location'])

    # ── Telegram ──
    def test_telegram_diagnose(self):
        orig = telegram._call
        info = {'url': '', 'pending_update_count': 3}

        def fake(m, p=None, timeout=30):
            if m == 'getMe':
                return {'ok': True, 'result': {'username': 'apqmsbot'}}
            return {'ok': True, 'result': info}
        telegram._call = fake
        try:
            txt = ' '.join(t for _, t in telegram.diagnose('https://x.example'))
            self.assertIn('@apqmsbot', txt)
            self.assertIn('register NAHI', txt)
            self.assertIn('3 message', txt)
            info.update({'url': 'https://x.example/api/v1/telegram/webhook',
                         'last_error_message': 'Wrong response from the webhook: 403 Forbidden',
                         'last_error_date': 1700000000})
            rep = telegram.diagnose('https://x.example')
            self.assertTrue(rep[1][0])
            self.assertIn('WEBHOOK_SECRET', ' '.join(t for _, t in rep))
        finally:
            telegram._call = orig

    def test_telegram_webhook(self):
        sent = []
        orig_call, orig_doc = telegram._call, telegram._send_document
        telegram._call = lambda m, p=None, timeout=30: sent.append((m, p)) or {'ok': True}
        telegram._send_document = lambda chat, name, content, caption='': sent.append(('doc', name, content[:4]))
        try:
            s, b = self.api('POST', '/api/v1/telegram/webhook', {'message': {'chat': {'id': 111}, 'text': 'hi'}},
                            token=False)
            self.assertEqual(s, 403)                              # no secret header
            hdr = {'X-Telegram-Bot-Api-Secret-Token': 'hook-secret'}
            s, b = self.api('POST', '/api/v1/telegram/webhook',
                            {'message': {'chat': {'id': 999}, 'text': 'help'}}, token=False, headers=hdr)
            self.assertEqual(s, 200)
            self.assertIn('999', sent[-1][1]['text'])             # stranger is told their chat id only
            sent.clear()
            s, b = self.api('POST', '/api/v1/telegram/webhook', {'message': {'chat': {'id': 111},
                            'text': 'proforma balaji: 3 pcs sprocket 12T @ 500'}}, token=False, headers=hdr)
            self.assertEqual(s, 200)
            texts = [p['text'] for m, p in [x for x in sent if x[0] == 'sendMessage']]
            self.assertTrue(any('Proforma API-' in t for t in texts), texts)
            self.assertTrue(any(x[0] == 'doc' and x[2] == b'%PDF' for x in sent))
        finally:
            telegram._call, telegram._send_document = orig_call, orig_doc

    # ── CLI ──
    def test_cli_local(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = apqms_cli.main(['create', 'quotation', '--customer', 'Balaji Engineering',
                                 '--item', 'roller chain 25.4|3', '--item', 'Gear box|1|15000|Nos.'])
        self.assertEqual(rc, 0)
        self.assertIn('Grand Total', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            apqms_cli.main(['chat', 'customer', 'balaji'])
        self.assertIn('Balaji Engineering Works', out.getvalue())

    def test_cli_remote(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = apqms_cli.main(['--url', self.base, '--token', self.token, 'chat', 'item', 'bearing'])
        self.assertEqual(rc, 0)
        self.assertIn('Ball Bearing 6205', out.getvalue())

    def test_revoked_token(self):
        tid, tok = tokens.create_token(self.admin['id'], 'temp')
        self.assertEqual(self.api('GET', '/api/v1/me', token=tok)[0], 200)
        tokens.revoke_token(tid)
        self.assertEqual(self.api('GET', '/api/v1/me', token=tok)[0], 401)


if __name__ == '__main__':
    unittest.main()
