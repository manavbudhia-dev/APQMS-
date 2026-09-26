"""Tests for app/ocr/engine.py, driven through the parser, on generated
sample documents (tests/make_fixtures.py). The OCR tests are skipped when the
Tesseract program isn't installed; everything else runs anywhere."""
import io
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

from app.ocr import engine, parser

HAVE_TESSERACT = engine.capabilities()['tesseract']
HAVE_PDF, HAVE_DOCX = engine.capabilities()['pdf'], engine.capabilities()['docx']
ENQUIRY = [(120, 'Mtr.'), (50, 'Mtr.'), (30, 'Mtr.')]


class Sniffing(unittest.TestCase):
    def test_types_are_detected_by_content(self):
        self.assertEqual(engine.sniff_file_type(b'%PDF-1.7 ...')[0], 'pdf')
        self.assertEqual(engine.sniff_file_type(b'\x89PNG\r\n\x1a\n....')[0], 'image')
        self.assertEqual(engine.sniff_file_type(b'\xff\xd8\xff\xe0....')[0], 'image')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('word/document.xml', '<x/>')
        self.assertEqual(engine.sniff_file_type(buf.getvalue()), ('docx', '.docx'))

    def test_rejections_explain_themselves(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('xl/workbook.xml', '<x/>')
        for data, word in ((buf.getvalue(), 'Word'), (b'\xd0\xcf\x11\xe0' + b'0' * 50, '.doc'),
                           (b'MZ\x90\x00', 'Unsupported'), (b'PK\x03\x04garbage', 'damaged')):
            kind, reason = engine.sniff_file_type(data)
            self.assertIsNone(kind)
            self.assertIn(word, reason)


@unittest.skipUnless(HAVE_PDF and HAVE_DOCX, 'pdfplumber / python-docx not installed')
class Documents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_fixtures
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = make_fixtures.build_all(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, name, kind, force=False):
        with open(os.path.join(self.dir, name), 'rb') as f:
            res = engine.extract(f.read(), kind, force_ocr=force)
        return res, parser.parse(res.lines)

    def check_enquiry(self, doc):
        self.assertEqual(doc.mode, 'table')
        self.assertEqual([(i.quantity, i.unit) for i in doc.items], ENQUIRY)
        self.assertEqual(doc.company_name, 'KRISHNA SUGAR MILLS LTD.')
        self.assertEqual(doc.gstin, '03AABCK1234F1Z5')
        self.assertEqual(doc.reference, 'KSM/PUR/2026/045')
        self.assertEqual(doc.date, '12 - 09 - 2026')
        self.assertEqual(doc.subject, 'Chains')
        self.assertIn('Plate thickness 5 MM', doc.items[0].description)   # wrapped cell kept together

    def test_pdf_with_text_layer_needs_no_ocr(self):
        res, doc = self.read('enquiry_table.pdf', 'pdf')
        self.assertEqual((res.method, res.avg_confidence), ('text_layer', None))
        self.check_enquiry(doc)

    def test_docx_table_and_header_letterhead(self):
        res, doc = self.read('enquiry.docx', 'docx')
        self.assertEqual(res.method, 'docx')
        self.check_enquiry(doc)                       # letterhead is in the Word page header

    def test_purchase_order_rates_amounts_and_total_row(self):
        _, doc = self.read('purchase_order.pdf', 'pdf')
        self.assertEqual([(i.quantity, i.rate, i.amount) for i in doc.items],
                         [(40, 3200, 128000), (25, 2850, 71250)])
        self.assertEqual((doc.company_name, doc.reference, doc.gstin),
                         ('Shree Ganesh Sugar Industries Pvt. Ltd.', 'PO/2026/778', '03AAACS4567L1ZK'))

    def test_freeform_letter(self):
        _, doc = self.read('freeform.pdf', 'pdf')
        self.assertEqual(doc.mode, 'freeform')
        self.assertEqual([i.quantity for i in doc.items], [120, 50, 80])
        self.assertEqual(doc.company_name, 'Bharat Agro Industries Ltd.')

    @unittest.skipUnless(HAVE_TESSERACT, 'Tesseract not installed')
    def test_scanned_pdf_matches_the_digital_original(self):
        res, doc = self.read('enquiry_table_scan.pdf', 'pdf')
        self.assertEqual(res.method, 'ocr')
        self.assertGreater(res.avg_confidence, 75)
        self.check_enquiry(doc)                       # incl. GSTIN 'O3AAB...' corrected to '03AAB...'

    @unittest.skipUnless(HAVE_TESSERACT, 'Tesseract not installed')
    def test_scanned_purchase_order_image(self):
        _, doc = self.read('purchase_order_scan.png', 'image')
        self.assertEqual([(i.quantity, i.rate, i.amount) for i in doc.items],
                         [(40, 3200, 128000), (25, 2850, 71250)])

    @unittest.skipUnless(HAVE_TESSERACT, 'Tesseract not installed')
    def test_sideways_and_upside_down_scans_are_straightened(self):
        for name in ('freeform_sideways.png', 'freeform_upside_down.png'):
            with self.subTest(name):
                _, doc = self.read(name, 'image')
                self.assertEqual([i.quantity for i in doc.items], [120, 50, 80])
                self.assertEqual(doc.company_name, 'Bharat Agro Industries Ltd.')

    @unittest.skipUnless(HAVE_TESSERACT, 'Tesseract not installed')
    def test_force_ocr_on_a_digital_pdf(self):
        res, doc = self.read('enquiry_table.pdf', 'pdf', force=True)
        self.assertEqual(res.method, 'ocr')
        self.assertEqual([(i.quantity, i.unit) for i in doc.items], ENQUIRY)

    def test_unreadable_input_gives_user_safe_errors(self):
        with self.assertRaises(engine.OcrError):
            engine.extract(b'%PDF-1.4 garbage', 'pdf')
        with self.assertRaises(engine.OcrError):
            engine.extract(b'PK\x03\x04 not really', 'docx')
        blank = io.BytesIO()
        from PIL import Image
        Image.new('RGB', (600, 400), 'white').save(blank, 'PNG')
        if HAVE_TESSERACT:
            with self.assertRaisesRegex(engine.OcrError, 'No readable text'):
                engine.extract(blank.getvalue(), 'image')

    def test_zip_bomb_docx_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('word/document.xml', '<x/>')
            z.writestr('word/pad.bin', b'\0' * (engine.MAX_DOCX_UNCOMPRESSED + 1))
        with self.assertRaisesRegex(engine.OcrError, 'unreasonable size'):
            engine.extract(buf.getvalue(), 'docx')


if __name__ == '__main__':
    unittest.main()
