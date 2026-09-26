"""
Builds realistic sample documents for testing the Scan & Import module.

Run directly to write them to a folder:   python tests/make_fixtures.py OUT_DIR
Imported by the test-suite, which calls build_all(tmpdir).

Everything here is synthetic (invented customers) — no real business data.
"""
import io
import os
import random
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ST = getSampleStyleSheet()
N = ParagraphStyle('n', parent=ST['Normal'], fontSize=10, leading=13)
B = ParagraphStyle('b', parent=N, fontName='Helvetica-Bold')
CELL = ParagraphStyle('c', parent=N, fontSize=9.5, leading=12)


def _letterhead():
    return [
        Paragraph('KRISHNA SUGAR MILLS LTD.', ParagraphStyle('h', parent=B, fontSize=16, leading=20)),
        Paragraph('Village Bhogpur, District Jalandhar, Punjab - 144201', N),
        Paragraph('Phone: 0181-2456789 &nbsp; Email: purchase@krishnasugar.in', N),
        Paragraph('GSTIN: 03AABCK1234F1Z5', N),
        Spacer(1, 14),
    ]


def enquiry_table_pdf(path):
    """Customer enquiry on the customer's letterhead, addressed to us,
    ruled items table WITHOUT rates (typical enquiry)."""
    rows = [['S.No', 'Description', 'Qty', 'Unit'],
            ['1', [Paragraph('Sugar Elevator Chain 66.60 MM Pitch', CELL),
                   Paragraph('Plate thickness 5 MM', CELL)], '120', 'Mtr.'],
            ['2', Paragraph('Roller Chain 25.40 MM Pitch', CELL), '50', 'Mtr.'],
            ['3', Paragraph('Crank Chain for Sugar Mills 76.20 MM Pitch', CELL), '30', 'Mtr.']]
    t = Table(rows, colWidths=[40, 300, 60, 60])
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                           ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                           ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story = _letterhead() + [
        Paragraph('Ref No: KSM/PUR/2026/045 &nbsp;&nbsp;&nbsp;&nbsp; Date: 12-09-2026', N), Spacer(1, 10),
        Paragraph('To,', N), Paragraph('M/s Aypee Industries', B),
        Paragraph('Near Chadha Engg. Works, Jalandhar - 144008', N), Spacer(1, 10),
        Paragraph('Sub: Enquiry for Chains', B), Spacer(1, 8),
        Paragraph('Dear Sir,', N), Spacer(1, 6),
        Paragraph('Please quote your best rates for the following items:', N), Spacer(1, 8), t,
        Spacer(1, 12), Paragraph('Thanking you,', N), Paragraph('Yours faithfully,', N), Spacer(1, 16),
        Paragraph('Mr. Rajesh Kumar', B), Paragraph('Purchase Manager', N)]
    SimpleDocTemplate(path, pagesize=A4).build(story)


def purchase_order_pdf(path):
    """Purchase order with rate + amount columns, a total row and terms."""
    rows = [['Sr.', 'Item Description', 'HSN', 'Qty', 'UOM', 'Rate', 'Amount'],
            ['1', Paragraph('Double Strand Pintle Chain 50.80 MM Pitch', CELL), '7315', '40', 'Mtr.', '3,200.00', '1,28,000.00'],
            ['2', Paragraph('Bagasse Carrier Chain 152.40 MM Pitch', CELL), '7315', '25', 'Mtr.', '2,850.00', '71,250.00'],
            ['', 'Total', '', '', '', '', '1,99,250.00']]
    t = Table(rows, colWidths=[30, 190, 40, 40, 40, 60, 75])
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                           ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold')]))
    story = [
        Paragraph('PURCHASE ORDER', ParagraphStyle('t', parent=B, fontSize=15, leading=19)),
        Paragraph('Buyer: Shree Ganesh Sugar Industries Pvt. Ltd.', B),
        Paragraph('Plot 12, Industrial Area, Phase 2, Ludhiana - 141003', N),
        Paragraph('Phone: 9876543210', N),
        Paragraph('GSTIN: 03AAACS4567L1ZK', N), Spacer(1, 8),
        Paragraph('PO No: PO/2026/778 &nbsp;&nbsp; Dated: 05/09/2026', N),
        Paragraph('Supplier: M/s Aypee Industries, Jalandhar. GSTIN: 03AAHFA4106G1Z4', N),
        Spacer(1, 10), t, Spacer(1, 10),
        Paragraph('Terms: Payment 30 days. Delivery within 6 weeks.', N)]
    SimpleDocTemplate(path, pagesize=A4).build(story)


def freeform_pdf(path):
    """No table at all: a numbered list in a letter, customer in a To-block."""
    story = [
        Paragraph('To,', N), Paragraph('The Purchase Manager', N),
        Paragraph('M/s Bharat Agro Industries Ltd.', B),
        Paragraph('Industrial Focal Point, Phase 5', N), Paragraph('Ludhiana - 141010', N),
        Paragraph('Mobile: 98765 43210', N), Spacer(1, 10),
        Paragraph('Sub: Enquiry for Chains', B), Spacer(1, 6), Paragraph('Dear Sir,', N), Spacer(1, 6),
        Paragraph('Kindly send your best quotation for the following:', N), Spacer(1, 6),
        Paragraph('1. Sugar Elevator Chain 66.60 mm pitch - 120 Mtrs', N),
        Paragraph('2. Roller Chain 25.4 mm pitch - 50 Mtrs', N),
        Paragraph('3. Bucher Harvesting Chain 101.60 mm pitch, 80 Mtr', N), Spacer(1, 10),
        Paragraph('Thanking you,', N), Paragraph('Yours faithfully,', N), Spacer(1, 14),
        Paragraph('Harpreet Singh', N), Paragraph('Purchase Manager', N)]
    SimpleDocTemplate(path, pagesize=A4).build(story)


def enquiry_docx(path):
    """The table enquiry as a Word file, with the letterhead in the page
    header (where Word letterheads live) and a multi-line description cell."""
    from docx import Document
    d = Document()
    hp = d.sections[0].header.paragraphs[0]
    hp.text = 'KRISHNA SUGAR MILLS LTD.'
    d.sections[0].header.add_paragraph('Village Bhogpur, District Jalandhar, Punjab - 144201')
    d.sections[0].header.add_paragraph('GSTIN: 03AABCK1234F1Z5')
    d.add_paragraph('Ref No: KSM/PUR/2026/045          Date: 12-09-2026')
    d.add_paragraph('To,')
    d.add_paragraph('M/s Aypee Industries')
    d.add_paragraph('Sub: Enquiry for Chains')
    d.add_paragraph('Please quote your best rates for the following items:')
    t = d.add_table(rows=1, cols=4)
    t.style = 'Table Grid'
    for c, h in zip(t.rows[0].cells, ['S.No', 'Description', 'Qty', 'Unit']):
        c.text = h
    for row in [('1', 'Sugar Elevator Chain 66.60 MM Pitch\nPlate thickness 5 MM', '120', 'Mtr.'),
                ('2', 'Roller Chain 25.40 MM Pitch', '50', 'Mtr.'),
                ('3', 'Crank Chain for Sugar Mills 76.20 MM Pitch', '30', 'Mtr.')]:
        cells = t.add_row().cells
        for c, v in zip(cells, row):
            c.text = v
    d.add_paragraph('')
    d.add_paragraph('Yours faithfully,')
    d.add_paragraph('Mr. Rajesh Kumar')
    d.add_paragraph('Purchase Manager')
    d.save(path)


# ── Turning a PDF into a realistic "scan" ────────────────────────────
def scan_of(pdf_path, out_pdf=None, out_png=None, angle=0.6, noise=True, dpi=200, seed=7):
    """Rasterize a PDF, add a slight tilt, grey background and speckle noise,
    and save as an image-only PDF and/or PNG — i.e. no text layer at all."""
    import pdfplumber
    from PIL import Image, ImageFilter
    rnd = random.Random(seed)
    with pdfplumber.open(pdf_path) as pdf:
        img = pdf.pages[0].to_image(resolution=dpi).original.convert('L')
    if angle:
        img = img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    if noise:
        px = img.load()
        for _ in range(img.width * img.height // 400):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = rnd.choice((0, 60, 200))
        img = img.point(lambda v: int(v * 0.93 + 8)).filter(ImageFilter.GaussianBlur(0.6))
    if out_pdf:
        img.convert('RGB').save(out_pdf, 'PDF', resolution=dpi)
    if out_png:
        img.save(out_png)
    return img


def rotate_png(src_png, out_png, degrees):
    from PIL import Image
    Image.open(src_png).rotate(degrees, expand=True, fillcolor=255).save(out_png)


def build_all(out):
    os.makedirs(out, exist_ok=True)
    p = lambda name: os.path.join(out, name)
    enquiry_table_pdf(p('enquiry_table.pdf'))
    purchase_order_pdf(p('purchase_order.pdf'))
    freeform_pdf(p('freeform.pdf'))
    enquiry_docx(p('enquiry.docx'))
    scan_of(p('enquiry_table.pdf'), out_pdf=p('enquiry_table_scan.pdf'), out_png=p('enquiry_table_scan.png'))
    scan_of(p('freeform.pdf'), out_pdf=p('freeform_scan.pdf'), out_png=p('freeform_scan.png'), angle=-0.8)
    scan_of(p('purchase_order.pdf'), out_png=p('purchase_order_scan.png'), angle=0.4)
    rotate_png(p('freeform_scan.png'), p('freeform_sideways.png'), 90)
    rotate_png(p('freeform_scan.png'), p('freeform_upside_down.png'), 180)
    return out


if __name__ == '__main__':
    print('Wrote fixtures to', build_all(sys.argv[1] if len(sys.argv) > 1 else 'fixtures'))
