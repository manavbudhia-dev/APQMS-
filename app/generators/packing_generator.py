"""Generate packing list PDF using ReportLab Platypus framework.

Layout is a deliberate structural replica of proforma_generator.py — same
header typography scale, same "No. ___/ Date ___" line pattern, same
totals-row and notes-box treatment — with the invoice-only content
(pricing, GST, payment request, bank details) swapped out for what a
packing list actually needs (quantities, bags/packages, bag breakdown).
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfgen import canvas
import io
import math

PAGE_W, PAGE_H = A4
MARGIN = 0.5 * inch
CONTENT_W = PAGE_W - 2 * MARGIN


def _rl_multiline(text):
    """Escape text for ReportLab's Paragraph XML markup and turn newlines
    into <br/> tags, so a multi-line customer address (entered line by
    line) renders as separate lines instead of one run-on line."""
    if not text:
        return ''
    escaped = (text.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
    return escaped.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br/>')


def _format_notes(text):
    """Escape notes text for Paragraph markup, turn newlines into <br/>,
    and convert **word** style markers into bold <b> tags — identical to
    the proforma invoice's note formatting, so notes look the same on
    both documents."""
    if not text:
        return ''
    escaped = (text.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
    escaped = escaped.replace('\r\n', '\n').replace('\r', '\n')
    segments = escaped.split('**')
    rebuilt = []
    for i, seg in enumerate(segments):
        rebuilt.append(f'<b>{seg}</b>' if i % 2 == 1 else seg)
    return ''.join(rebuilt).replace('\n', '<br/>')


def _fmt_qty(qty):
    """Render a quantity without a trailing .0 for whole numbers."""
    return str(int(qty)) if qty == int(qty) else str(qty)


def _build_header_flowables(pl_date=''):
    """Build the header content as a list of flowables — font sizes,
    spacing and structure match proforma_generator._build_header_flowables
    exactly, so both documents share one letterhead style."""
    flowables = []

    # GSTIN line
    gstin_style = ParagraphStyle('gstin', fontName='Helvetica', fontSize=10,
                                  alignment=TA_LEFT, leading=12)
    flowables.append(Paragraph("GSTIN: 03AAHFA4106G1Z4", gstin_style))
    flowables.append(Spacer(1, 4))

    title_style = ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=14,
                                  alignment=TA_CENTER, leading=17)
    flowables.append(Paragraph("PACKING LIST", title_style))
    flowables.append(Spacer(1, 2))

    company_style = ParagraphStyle('company', fontName='Helvetica-Bold', fontSize=28,
                                    alignment=TA_CENTER, leading=32,
                                    textColor=HexColor('#FF0000'))
    flowables.append(Paragraph("AYPEE INDUSTRIES", company_style))
    flowables.append(Spacer(1, 6))

    # --- 3-column address / CHAINS / contact table ---
    addr_style = ParagraphStyle('addr', fontName='Helvetica', fontSize=9, leading=11)
    addr_html = (
        "<b><u>Correspondence Address:</u></b><br/>"
        "STREET NO.1, ANAND NAGAR<br/>"
        "NEAR CHADHA ENGG. WORKS,<br/>"
        "MAKSUDAN, JALANDHAR<br/>"
        "(PUNJAB), INDIA<br/>"
        "PIN CODE &#8211; 144008"
    )
    addr_para = Paragraph(addr_html, addr_style)

    chains_style = ParagraphStyle('chains', fontName='Helvetica-Bold', fontSize=16,
                                   textColor=HexColor('#FF0000'), alignment=TA_CENTER)
    chains_para = Paragraph("CHAINS", chains_style)

    contact_style = ParagraphStyle('contact', fontName='Helvetica', fontSize=10, leading=13)
    contact_html = "<b>(Off.):</b> 0181-2672260<br/><br/><b>(Mob):</b> 9357255758, 9041881996"
    contact_para = Paragraph(contact_html, contact_style)

    addr_table = Table(
        [[addr_para, chains_para, contact_para]],
        colWidths=[CONTENT_W * 0.44, CONTENT_W * 0.22, CONTENT_W * 0.34],
    )
    addr_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('VALIGN', (1, 0), (1, 0), 'MIDDLE'),
        ('VALIGN', (2, 0), (2, 0), 'BOTTOM'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (1, 0), (1, 0), 8),
        ('RIGHTPADDING', (1, 0), (1, 0), 8),
        ('LEFTPADDING', (2, 0), (2, 0), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LINEABOVE', (0, 0), (-1, 0), 1, black),
        ('LINEBELOW', (0, 0), (-1, 0), 1, black),
        ('LINEBEFORE', (1, 0), (1, 0), 1.2, HexColor('#FF0000')),
        ('LINEAFTER', (1, 0), (1, 0), 1.2, HexColor('#FF0000')),
    ]))
    flowables.append(addr_table)
    flowables.append(Spacer(1, 6))

    mfg_style = ParagraphStyle('mfg', fontName='Helvetica-Bold', fontSize=10,
                                alignment=TA_CENTER, leading=12)
    flowables.append(Paragraph(
        "MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, "
        "RBC &amp; RAKE CARRIER CHAINS, SLAT &amp; SPARE PARTS",
        mfg_style
    ))
    flowables.append(Spacer(1, 3))

    email_style = ParagraphStyle('email', fontName='Helvetica', fontSize=10,
                                  alignment=TA_CENTER)
    email_para = Paragraph(
        'E-mail: <u><font color="#0000FF">aypeeinds@yahoo.com</font></u>',
        email_style
    )

    date_field_style = ParagraphStyle('date_field', fontName='Helvetica-Bold', fontSize=10,
                                       alignment=TA_RIGHT)
    date_field_para = Paragraph('Date ___________', date_field_style)

    email_date_table = Table(
        [['', email_para, date_field_para]],
        colWidths=[CONTENT_W * 0.30, CONTENT_W * 0.40, CONTENT_W * 0.30],
    )
    email_date_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    flowables.append(email_date_table)

    return flowables


def _measure_header_height(pl_date=''):
    """Wrap every header flowable at CONTENT_W to compute the total header height."""
    total_h = 0
    for f in _build_header_flowables(pl_date):
        _, h = f.wrap(CONTENT_W, PAGE_H)
        total_h += h
    return total_h


def _make_draw_header(pl_date=''):
    """Return a canvas callback bound to the given date."""
    def _draw_header(pdf_canvas, doc):
        pdf_canvas.saveState()
        y = PAGE_H - MARGIN
        for f in _build_header_flowables(pl_date):
            w, h = f.wrap(CONTENT_W, y)
            y -= h
            f.drawOn(pdf_canvas, MARGIN, y)
        pdf_canvas.restoreState()
    return _draw_header


def build_packing_pdf(packing_list, customer, items):
    """Build packing list PDF using Platypus framework."""
    buf = io.BytesIO()

    pl_date = packing_list.get('date', '')

    # Measure the header first so the body content never overlaps it
    header_height = _measure_header_height(pl_date)
    top_margin = MARGIN + header_height + 0.15 * inch

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=top_margin,
        bottomMargin=MARGIN
    )

    # Define styles — same scale as proforma_generator (10pt body / 13 leading)
    styles = getSampleStyleSheet()

    style_normal = ParagraphStyle(
        'CustomNormal', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, leading=13
    )
    style_bold = ParagraphStyle(
        'CustomBold', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=10, leading=13
    )
    style_center = ParagraphStyle('CustomCenter', parent=style_normal, alignment=TA_CENTER)
    style_center_bold = ParagraphStyle('CustomCenterBold', parent=style_bold, alignment=TA_CENTER)
    style_customer_name = ParagraphStyle('CustomerName', parent=style_bold, alignment=TA_CENTER,
                                          fontSize=13, leading=16)

    story = []

    # Packing List No. line — mirrors the proforma's "Order No. / Order
    # Date" row: PL number on the left, actual PL date on the right (or a
    # blank fill-in line when no date is set).
    pl_no = _rl_multiline(packing_list.get('pl_no', ''))
    order_line = f"<b>Packing List No. ____{pl_no}____</b>"
    if pl_date:
        order_line += (
            " " * 12
            + f"<b>Date ____{_rl_multiline(pl_date)}____</b>"
        )
    else:
        order_line += (
            " " * 50
            + "<b>Dated ____________</b>"
        )
    story.append(Paragraph(order_line, style_bold))
    story.append(Spacer(1, 0.1 * inch))

    if customer:
        company_name = _rl_multiline(customer.get('company_name', '').upper())
        buyer_line = f"<b><u>M/s.___ {company_name}___</u></b>"
        story.append(Paragraph(buyer_line, style_customer_name))
        story.append(Spacer(1, 0.05 * inch))

        address = _rl_multiline(customer.get('address', '').upper())
        addr_line = f"<b><u>__________{address}__________________</u></b>"
        story.append(Paragraph(addr_line, style_center_bold))
        story.append(Spacer(1, 0.15 * inch))

    # Line items table - with bag breakdown
    # For each item, show: S.No., Particulars, Total Qty, Unit, Qty/Bag, Total Bags, Bag Breakdown
    table_data = [['S.No.', 'Particulars', 'Total Qty', 'Unit', 'Qty/Bag', 'Bags', 'Bag Breakdown']]

    qty_by_unit = {}
    total_bags_sum = 0

    for idx, item in enumerate(items, 1):
        total_qty = item.get('total_qty', 0)
        bag_qty = item.get('bag_qty', 0) or total_qty

        # Calculate bags
        if bag_qty > 0:
            total_bags = math.ceil(total_qty / bag_qty)
            full_bags = int(total_qty // bag_qty)
            last_bag = total_qty - (full_bags * bag_qty)
        else:
            total_bags = 1
            full_bags = 0
            last_bag = total_qty

        # Build bag breakdown string
        breakdown_parts = []
        if full_bags > 0:
            breakdown_parts.append(f"{full_bags} x {int(bag_qty) if bag_qty == int(bag_qty) else bag_qty}")
        if last_bag > 0 and last_bag != bag_qty:
            breakdown_parts.append(f"1 x {int(last_bag) if last_bag == int(last_bag) else last_bag}")
        elif last_bag > 0 and full_bags == 0:
            breakdown_parts.append(f"1 x {int(last_bag) if last_bag == int(last_bag) else last_bag}")

        bag_breakdown = " + ".join(breakdown_parts) if breakdown_parts else str(int(total_qty))

        item_name = _rl_multiline(item.get('item_name', ''))
        description = _rl_multiline(item.get('description', ''))

        particulars_html = f"<b>{item_name}</b>"
        if description:
            particulars_html += f"<br/><i>{description}</i>"

        particulars = Paragraph(particulars_html, ParagraphStyle(
            'ItemStyle', parent=styles['Normal'],
            fontName='Helvetica', fontSize=9, leading=11
        ))

        unit = item.get('unit', 'Pcs')
        qty_by_unit[unit] = qty_by_unit.get(unit, 0) + total_qty
        total_bags_sum += total_bags

        row = [
            str(idx),
            particulars,
            _fmt_qty(total_qty),
            unit,
            _fmt_qty(bag_qty),
            str(total_bags),
            bag_breakdown
        ]
        table_data.append(row)

    # Totals row — the packing-list equivalent of the proforma's
    # Net/GST/Total block: total quantity (grouped by unit, since a
    # packing list can mix Pcs/Kgs/etc.) and total number of bags,
    # right-aligned and bold, with the grand total picked out in red
    # exactly like "Total Amount Payable" on the invoice.
    qty_total_str = ', '.join(f"{_fmt_qty(q)} {u}" for u, q in qty_by_unit.items())
    table_data.append(['', 'Total Quantity:', qty_total_str, '', '', '', ''])
    table_data.append(['', 'Total No. of Bags/Packages:', '', '', '', str(total_bags_sum), ''])

    col_widths = [0.4 * inch, 2.3 * inch, 0.65 * inch, 0.55 * inch, 0.65 * inch, 0.5 * inch, 2.22 * inch]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)

    n_items = len(items)
    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#D9D9D9')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, black),
        ('ALIGN', (0, 1), (0, n_items), 'CENTER'),
        ('ALIGN', (2, 1), (2, n_items), 'CENTER'),
        ('ALIGN', (3, 1), (3, n_items), 'CENTER'),
        ('ALIGN', (4, 1), (4, n_items), 'CENTER'),
        ('ALIGN', (5, 1), (5, n_items), 'CENTER'),
        ('ALIGN', (6, 1), (6, n_items), 'CENTER'),
        ('FONTNAME', (0, 1), (-1, n_items), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, n_items), 9),
    ]

    # Style the two totals rows (mirrors proforma's totals_rows loop)
    qty_row = n_items + 1
    bags_row = n_items + 2
    for row_idx in (qty_row, bags_row):
        table_style.append(('SPAN', (1, row_idx), (1, row_idx)))
        table_style.append(('SPAN', (2, row_idx), (4, row_idx)))
        table_style.append(('ALIGN', (1, row_idx), (1, row_idx), 'LEFT'))
        table_style.append(('ALIGN', (2, row_idx), (4, row_idx), 'RIGHT'))
        table_style.append(('ALIGN', (5, row_idx), (5, row_idx), 'CENTER'))
        table_style.append(('FONTNAME', (1, row_idx), (-1, row_idx), 'Helvetica-Bold'))
        table_style.append(('FONTSIZE', (1, row_idx), (-1, row_idx), 10))
    table_style.append(('TEXTCOLOR', (5, bags_row), (5, bags_row), HexColor('#FF0000')))

    items_table.setStyle(TableStyle(table_style))
    story.append(items_table)
    story.append(Spacer(1, 0.15 * inch))

    # ── NOTES (printed if present; wrap words in **double-asterisks** to
    #    bold/highlight just that part) — same treatment as the proforma
    #    invoice's notes box, previously missing here despite the field
    #    existing on every packing list. ──
    notes_text = packing_list.get('notes', '')
    if notes_text:
        notes_style = ParagraphStyle('NotesStyle', parent=styles['Normal'],
                                      fontName='Helvetica', fontSize=10, leading=13)
        notes_para = Paragraph(
            f"<b>Note:</b> {_format_notes(notes_text)}", notes_style
        )
        notes_table = Table([[notes_para]], colWidths=[CONTENT_W])
        notes_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#FFF9DB')),
            ('BOX', (0, 0), (-1, -1), 0.75, HexColor('#FFD43B')),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(notes_table)
        story.append(Spacer(1, 0.15 * inch))

    # Footer — pricing/GST/payment-request/bank-details sections from the
    # proforma are intentionally omitted here since a packing list carries
    # no monetary content; the jurisdiction line and signature block are
    # kept identical.
    story.append(Paragraph("Subject to Jalandhar Jurisdiction", style_normal))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("<b>For AYPEE INDUSTRIES</b>", style_bold))
    story.append(Spacer(1, 0.15 * inch))

    signature_right = ParagraphStyle('SignatureRight', parent=style_bold, alignment=TA_RIGHT)
    story.append(Paragraph("<b>Raman Budhia (Partner)</b>", signature_right))
    story.append(Paragraph("<b>+91-9357255758</b>", signature_right))

    draw_header = _make_draw_header(pl_date)
    doc.build(story, onFirstPage=draw_header, onLaterPages=draw_header)

    buf.seek(0)
    return buf.getvalue()
