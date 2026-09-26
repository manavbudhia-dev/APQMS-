"""End-to-end test of Scan & Import over real HTTP: the actual request
handler runs in-process on a random port with a throwaway database, upload
folder and session files, and a browser-like client drives it — login,
upload, review form, create quotation, and the ways it must refuse."""
import io
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

# Point the app at a throwaway database BEFORE it is imported (DB_PATH is
# read at import time). ocr_uploads/ then lands in the same folder.
_TMP = tempfile.TemporaryDirectory()
os.environ['QUOTATION_DB'] = os.path.join(_TMP.name, 'test.db')
os.environ['ADMIN_PASSWORD'] = 'Test-Passw0rd!'

import http.server                                       # noqa: E402

from utils import http_utils                             # noqa: E402
for _attr, _name in (('_SESSIONS_FILE', 'sessions.json'), ('_CSRF_FILE', 'csrf.json'),
                     ('_ATTEMPTS_FILE', 'attempts.json')):
    setattr(http_utils, _attr, os.path.join(_TMP.name, _name))

import database                                          # noqa: E402
from app.ocr import engine, service                      # noqa: E402
from server import QuotationHandler                      # noqa: E402
from http_client import Client, scrape                   # noqa: E402
import make_fixtures                                     # noqa: E402

HAVE_OCR = engine.capabilities()['tesseract'] and engine.capabilities()['pdf'] and engine.capabilities()['docx']


@unittest.skipUnless(HAVE_OCR, 'Tesseract / OCR libraries not installed')
class ScanAndImportPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        cls.fx = make_fixtures.build_all(os.path.join(_TMP.name, 'fx'))
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuotationHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.c = Client(cls.base)
        assert cls.c.login('admin', os.environ['ADMIN_PASSWORD'])[0] == 302

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # ── helpers ──
    def fx_bytes(self, name):
        with open(os.path.join(self.fx, name), 'rb') as f:
            return f.read()

    def upload(self, name, data, client=None, token=None):
        c = client or self.c
        if token is None:
            token = c.csrf_from(c.get('/ocr')[2])
        return c.post_multipart('/ocr/upload', {'csrf_token': token},
                                {'file': (name, 'application/octet-stream', data)})

    def wait(self, loc, client=None):
        c = client or self.c
        for _ in range(90):
            s, _h, html = c.get(loc)
            if 'spinner-border' not in html:
                return html
            time.sleep(1)
        self.fail('document was still processing after 90s')

    def db(self):
        conn = sqlite3.connect(os.environ['QUOTATION_DB'])
        conn.row_factory = sqlite3.Row
        return conn

    def import_file(self, name):
        s, h, _ = self.upload(name, self.fx_bytes(name))
        self.assertEqual(s, 302, name)
        return h['Location'], self.wait(h['Location'])

    # ── tests ──
    def test_1_scan_to_quotation_then_customer_recognised(self):
        # a small catalog so items link and get rates
        with self.db() as conn:
            for n, d, r in (('SUGAR ELEVATOR CHAIN', 'Sugar Elevator Chain 66.60 MM Pitch', 2700),
                            ('ROLLER CHAIN', 'Roller Chain 25.40 MM Pitch', 550),
                            ('CRANK CHAIN', 'Crank Chain for Sugar Mills 76.20 MM Pitch', 2450)):
                conn.execute("INSERT INTO items (item_name, description, unit, rate_per_unit) VALUES (?,?,?,?)",
                             (n, d, 'Mtr.', r))
        loc, html = self.import_file('enquiry_table_scan.pdf')
        form = dict(scrape(html))
        self.assertEqual(form['company_name'], 'KRISHNA SUGAR MILLS LTD.')
        self.assertEqual(form['customer_id'], 'new')
        self.assertIn('Matched to catalog', html)

        s, h, _ = self.c.post_form(loc + '/quotation', scrape(html))
        self.assertEqual(s, 302)
        self.assertTrue(h['Location'].startswith('/quotations/'))
        with self.db() as conn:
            q = conn.execute("SELECT * FROM quotations WHERE id=?", (int(h['Location'].rsplit('/', 1)[1]),)).fetchone()
            items = conn.execute("SELECT item_name, quantity, unit, rate, item_id FROM quotation_items "
                                 "WHERE quotation_id=? ORDER BY sort_order", (q['id'],)).fetchall()
            cust = conn.execute("SELECT * FROM customers WHERE id=?", (q['customer_id'],)).fetchone()
        self.assertEqual(q['status'], 'Draft')
        self.assertEqual(q['subject'], 'Chains')
        self.assertIn('KSM/PUR/2026/045', q['notes'])
        self.assertEqual([(i['quantity'], i['unit'], i['rate']) for i in items],
                         [(120, 'Mtr.', 2700), (50, 'Mtr.', 550), (30, 'Mtr.', 2450)])
        self.assertTrue(all(i['item_id'] for i in items))
        self.assertEqual((cust['company_name'], cust['gstin']), ('KRISHNA SUGAR MILLS LTD.', '03AABCK1234F1Z5'))

        s, _h, pdf = self.c.req('GET', f"/quotations/{q['id']}/pdf")[0:3]
        self.assertEqual((s, pdf[:5]), (200, b'%PDF-'))                 # the normal PDF still renders

        # the same customer arriving again is recognised by GSTIN
        _loc2, html2 = self.import_file('enquiry_table_scan.pdf')
        self.assertIn('Matched to an existing customer by GSTIN', html2)
        self.assertEqual(dict(scrape(html2))['customer_id'], str(cust['id']))

    def test_2_incomplete_rows_keep_edits_but_refuse_the_quotation(self):
        loc, html = self.import_file('purchase_order.pdf')
        pairs, seen = [], 0
        for k, v in scrape(html):
            if k == 'rate[]':
                v, seen = ('' if seen == 1 else v), seen + 1
            pairs.append((k, 'thirty' if (k == 'quantity[]' and v == '25') else v))
        with self.db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
        s, _h, body = self.c.post_form(loc + '/quotation', [(k, v) if k != 'customer_id' else (k, 'new') for k, v in pairs])
        body = body.decode()
        self.assertEqual(s, 400)
        self.assertIn('needs a quantity', body)          # 'thirty' was cleared as not-a-number
        self.assertIn('not a number', body)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0], before)
            self.assertEqual(conn.execute("SELECT status FROM ocr_documents WHERE id=?",
                                          (int(loc.rsplit('/', 1)[1]),)).fetchone()[0], 'Saved')

    def test_3_save_only_stores_data_without_a_quotation(self):
        loc, html = self.import_file('freeform.pdf')
        with self.db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
        s, _h, body = self.c.post_form(loc + '/save', scrape(html))
        self.assertEqual((s, 'Data saved' in body.decode()), (200, True))
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0], before)
            n = conn.execute("SELECT COUNT(*) FROM ocr_document_items WHERE ocr_document_id=?",
                             (int(loc.rsplit('/', 1)[1]),)).fetchone()[0]
        self.assertEqual(n, 3)

    def test_4_word_document_and_sideways_photo(self):
        _l, html = self.import_file('enquiry.docx')
        self.assertIn('Read from the Word document', html)
        _l, html = self.import_file('freeform_sideways.png')
        self.assertIn('Bharat Agro', html)
        self.assertIn('check this row', html)

    def test_5_bad_uploads_are_refused_with_reasons(self):
        book = io.BytesIO()
        with zipfile.ZipFile(book, 'w') as z:
            z.writestr('xl/workbook.xml', 'x')
        for name, data, expect in (('evil.pdf', b'MZ\x90\x00 not a pdf', b'Unsupported file type'),
                                   ('sheet.docx', book.getvalue(), b'not a Word document'),
                                   ('old.doc', b'\xd0\xcf\x11\xe0' + b'0' * 64, b'.doc'),
                                   ('empty.pdf', b'', b'choose a file')):
            s, _h, b = self.upload(name, data)
            self.assertEqual(s, 400, name)
            self.assertIn(expect, b, name)
        s, _h, b = self.c.post_multipart('/ocr/upload', {'csrf_token': 'bogus'},
                                         {'file': ('a.pdf', 'application/pdf', self.fx_bytes('freeform.pdf'))})
        self.assertEqual((s, b'session expired' in b), (400, True))
        big = b'%PDF-1.4\n' + b'0' * (engine.MAX_UPLOAD_BYTES + 1024 * 1024 + 10)
        self.assertEqual(self.upload('big.pdf', big)[0], 413)
        # passes the content check, fails to parse: a friendly Failed page, no traceback
        s, h, _ = self.upload('corrupt.pdf', b'%PDF-1.4\n garbage')
        page = self.wait(h['Location'])
        self.assertIn('could not be', page)
        self.assertNotIn('Traceback', page)

    def test_6_hostile_content_is_escaped(self):
        from docx import Document
        d = Document()
        d.add_paragraph('M/s <script>alert(1)</script> Industries Ltd.')
        d.add_paragraph('Village <img src=x onerror=alert(2)>, Punjab - 144001')
        t = d.add_table(rows=2, cols=4)
        for cell, v in zip(t.rows[0].cells, ['S.No', 'Description', 'Qty', 'Unit']):
            cell.text = v
        for cell, v in zip(t.rows[1].cells, ['1', '<b>Chain</b> & <script>x</script> 50mm', '10', 'Mtr.']):
            cell.text = v
        buf = io.BytesIO()
        d.save(buf)
        s, h, _ = self.upload('"><script>alert(3)</script>.docx', buf.getvalue())
        page = self.wait(h['Location'])
        for live in ('<script>alert', '<img src=x', '<b>Chain</b>', '<script>x'):
            self.assertNotIn(live, page)
        self.assertNotIn('<script>alert', self.c.get('/ocr')[2])
        s, hd, _b = self.c.req('GET', h['Location'] + '/file')
        disp = hd.get('Content-Disposition', '')
        self.assertTrue(disp.startswith('attachment') and '<' not in disp and '>' not in disp, disp)

    def test_7_login_required_and_delete_cleans_up(self):
        loc, _html = self.import_file('freeform.pdf')
        anon = Client(self.base)
        for path in ('/ocr', loc, loc + '/file'):
            s, h, _ = anon.get(path)
            self.assertEqual((s, '/login' in (h.get('Location') or '')), (302, True), path)
        did = int(loc.rsplit('/', 1)[1])
        with self.db() as conn:
            stored = conn.execute("SELECT stored_filename FROM ocr_documents WHERE id=?", (did,)).fetchone()[0]
        self.assertTrue(os.path.isfile(os.path.join(service.UPLOAD_DIR, stored)))
        self.assertEqual(self.c.post_form(loc + '/delete', [])[0], 302)
        self.assertFalse(os.path.exists(os.path.join(service.UPLOAD_DIR, stored)))
        self.assertEqual(self.c.req('GET', loc + '/file')[0], 404)

    def test_8_deleting_a_customer_or_item_is_not_blocked_by_imports(self):
        # Imports reference customers/items; deleting those elsewhere in the app
        # must still work (the links are ON DELETE SET NULL) and just unlink.
        with self.db() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cid = conn.execute("INSERT INTO customers (contact_person, company_name) VALUES ('x','Temp Co')").lastrowid
            iid = conn.execute("INSERT INTO items (item_name) VALUES ('TEMP ITEM')").lastrowid
            did = conn.execute("INSERT INTO ocr_documents (original_filename, customer_id, status) "
                               "VALUES ('t.pdf', ?, 'Saved')", (cid,)).lastrowid
            conn.execute("INSERT INTO ocr_document_items (ocr_document_id, item_id, item_name) VALUES (?,?,'t')",
                         (did, iid))
            conn.commit()
            conn.execute("DELETE FROM customers WHERE id=?", (cid,))
            conn.execute("DELETE FROM items WHERE id=?", (iid,))
            conn.commit()
            self.assertIsNone(conn.execute("SELECT customer_id FROM ocr_documents WHERE id=?", (did,)).fetchone()[0])
            self.assertIsNone(conn.execute("SELECT item_id FROM ocr_document_items WHERE ocr_document_id=?",
                                           (did,)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
