"""Generate quotation PDF using ReportLab Platypus framework.
Header styling matches proforma_generator.py exactly (same fonts, colors,
red CHAINS divider lines, bottom-aligned contact info, dynamic top margin
so nothing ever overlaps).
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import io

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
    and convert **word** style markers into bold <b> tags so specific
    parts of a note can be highlighted without affecting the rest."""
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


def _indian_format(amount, decimals=2):
    """Format a number using the Indian numbering system (lakh/crore grouping:
    commas every 2 digits after the first 3), e.g. 2850000 -> '28,50,000.00'."""
    if amount < 0:
        sign = '-'
        amount = abs(amount)
    else:
        sign = ''

    if decimals > 0:
        int_part = int(amount)
        frac_part = round(amount - int_part, decimals)
        frac_str = f"{frac_part:.{decimals}f}".split('.')[1]
    else:
        int_part = int(round(amount))
        frac_str = None

    s = str(int_part)
    if len(s) <= 3:
        grouped = s
    else:
        last_three = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ','.join(parts) + ',' + last_three

    if frac_str is not None:
        return f"{sign}{grouped}.{frac_str}"
    return f"{sign}{grouped}"


# ---------------------------------------------------------------------------
# Header (identical styling to proforma_generator.py, title swapped to
# "QUOTATION" and Date filled with the real value instead of left blank)
# ---------------------------------------------------------------------------
def _build_header_flowables(invoice_date=''):
    flowables = []

    gstin_style = ParagraphStyle('gstin', fontName='Helvetica', fontSize=10,
                                  alignment=TA_LEFT, leading=12)
    flowables.append(Paragraph("GSTIN: 03AAHFA4106G1Z4", gstin_style))
    flowables.append(Spacer(1, 4))

    title_style = ParagraphStyle('title', fontName='Helvetica', fontSize=10,
                                  alignment=TA_CENTER, leading=12, underlineWidth=1)
    flowables.append(Paragraph("<u>Subject to Jalandhar Jurisdiction</u>", title_style))
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
    date_text = f"Date: {invoice_date}" if invoice_date else "Date ___________"
    date_field_para = Paragraph(date_text, date_field_style)

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


def _measure_header_height(invoice_date=''):
    total_h = 0
    for f in _build_header_flowables(invoice_date):
        _, h = f.wrap(CONTENT_W, PAGE_H)
        total_h += h
    return total_h


def _make_draw_header(invoice_date=''):
    def _draw_header(pdf_canvas, doc):
        pdf_canvas.saveState()
        y = PAGE_H - MARGIN
        for f in _build_header_flowables(invoice_date):
            w, h = f.wrap(CONTENT_W, y)
            y -= h
            f.drawOn(pdf_canvas, MARGIN, y)
        pdf_canvas.restoreState()
    return _draw_header


# ---------------------------------------------------------------------------
# Main build function — same signature as before: build_pdf(quotation, customer, items, company=None)
# ---------------------------------------------------------------------------
def build_pdf(quotation, customer, items, company=None):
    buf = io.BytesIO()

    invoice_date = quotation.get('date', '')

    header_height = _measure_header_height(invoice_date)
    top_margin = MARGIN + header_height + 0.15 * inch

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=top_margin,
        bottomMargin=MARGIN
    )

    styles = getSampleStyleSheet()

    style_normal = ParagraphStyle(
        'CustomNormal', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, leading=13
    )
    style_bold = ParagraphStyle(
        'CustomBold', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=10, leading=13
    )
    style_center_bold = ParagraphStyle('CustomCenterBold', parent=style_bold, alignment=TA_CENTER)
    style_subject = ParagraphStyle('Subject', parent=style_bold, alignment=TA_CENTER,
                                    fontSize=11, leading=14)

    story = []

    # ── TO (Customer) ──
    story.append(Paragraph("<b>To</b>", style_bold))
    if customer:
        story.append(Paragraph(f"<b>{customer.get('contact_person', '')}</b>", style_bold))
        story.append(Paragraph(f"<b>{customer.get('company_name', '')}</b>", style_bold))
        story.append(Paragraph(_rl_multiline(customer.get('address', '')), style_normal))
    story.append(Spacer(1, 0.12 * inch))

    # ── SUBJECT ──
    subj = quotation.get('subject', '')
    story.append(Paragraph(f"<u>Sub: Quotation for {subj}</u>", style_subject))
    story.append(Spacer(1, 0.1 * inch))

    # ── DEAR SIR + OPENING ──
    story.append(Paragraph("Dear Sir,", style_normal))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        f"We are pleased to quote you the best rates for your inquiry for {subj}.",
        style_normal
    ))
    story.append(Spacer(1, 0.18 * inch))

    # ── ITEM TABLE ──
    table_data = [['Sr. No.', 'Description', 'Quantity', 'Price']]

    item_style = ParagraphStyle('ItemStyle', parent=styles['Normal'],
                                 fontName='Helvetica', fontSize=9, leading=11)
    item_bold_style = ParagraphStyle('ItemBoldStyle', parent=styles['Normal'],
                                      fontName='Helvetica-Bold', fontSize=9, leading=11)

    for idx, item in enumerate(items, 1):
        qty = item.get('quantity', 0)
        rate = item.get('rate', 0)
        unit = item.get('unit', '')

        item_name_html = f"<b>{_rl_multiline(item.get('item_name', ''))}</b>"
        description_html = _rl_multiline(item.get('description', ''))
        desc_html = item_name_html + (f"<br/>{description_html}" if description_html else "")
        spec_text = item.get('specification', '')
        if spec_text:
            spec_lines = [ln.strip() for ln in spec_text.split('\n') if ln.strip()]
            desc_html += "<br/><b>Specification:</b><br/>" + "<br/>".join(spec_lines)
        desc_para = Paragraph(desc_html, item_style)

        qty_para = Paragraph(f"{qty} {unit}", item_style)
        unit_label = 'each' if unit.strip().upper().rstrip('.') == 'NOS' else f'per {unit}'
        price_para = Paragraph(
            f"@ Rs. {_indian_format(rate)}/-<br/>{unit_label}", item_style
        )

        table_data.append([
            Paragraph(str(idx) + '.', item_bold_style),
            desc_para,
            qty_para,
            price_para,
        ])

    col_widths = [0.5 * inch, 3.9 * inch, 1.0 * inch, 1.5 * inch]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#F5F5F5')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('VALIGN', (0, 1), (0, -1), 'TOP'),
        ('VALIGN', (1, 1), (1, -1), 'TOP'),
        ('VALIGN', (2, 1), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.5, black),
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 0.2 * inch))

    # ── NOTES (printed if present; wrap words in **double-asterisks** to
    #    bold/highlight just that part) ──
    notes_text = quotation.get('notes', '')
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
        story.append(Spacer(1, 0.2 * inch))

    # ── TERMS AND CONDITIONS ──
    story.append(Paragraph("<b>Terms and Conditions</b>", style_bold))
    story.append(Spacer(1, 0.05 * inch))

    terms = quotation.get('terms', '')
    term_num = 1
    for line in terms.split('\n'):
        line = line.strip()
        if line:
            clean = line
            for prefix in [f"{term_num}.", f"{term_num}. ", f"{term_num}.  "]:
                if clean.startswith(prefix):
                    clean = clean[len(prefix):]
                    break
            story.append(Paragraph(f"{term_num}.&nbsp;&nbsp;{clean.strip()}", style_normal))
            term_num += 1
    story.append(Spacer(1, 0.25 * inch))

    # ── SIGNATURE ──
    story.append(Paragraph("<b>Thanks &amp; Regards</b>", style_bold))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("<b>For Aypee Industries</b>", style_bold))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph("<b>Raman Budhia (Partner)</b>", style_bold))
    story.append(Paragraph("<b>+91-9357255758</b>", style_bold))

    draw_header = _make_draw_header(invoice_date)
    doc.build(story, onFirstPage=draw_header, onLaterPages=draw_header)

    buf.seek(0)
    return buf.getvalue()
