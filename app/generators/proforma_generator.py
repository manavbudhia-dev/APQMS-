"""Generate proforma invoice PDF using ReportLab Platypus framework."""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfgen import canvas
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


def _build_header_flowables(invoice_date=''):
    """Build the header content as a list of flowables (Paragraphs/Tables),
    used both for measuring total header height and for drawing it."""
    flowables = []

    # GSTIN line only (date now shown in the Order No. row instead)
    gstin_style = ParagraphStyle('gstin', fontName='Helvetica', fontSize=10,
                                  alignment=TA_LEFT, leading=12)
    flowables.append(Paragraph("GSTIN: 03AAHFA4106G1Z4", gstin_style))
    flowables.append(Spacer(1, 4))

    title_style = ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=14,
                                  alignment=TA_CENTER, leading=17)
    flowables.append(Paragraph("PROFORMA INVOICE", title_style))
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


def _measure_header_height(invoice_date=''):
    """Wrap every header flowable at CONTENT_W to compute the total header
    height, so the body's top margin always matches what actually gets drawn."""
    total_h = 0
    for f in _build_header_flowables(invoice_date):
        _, h = f.wrap(CONTENT_W, PAGE_H)
        total_h += h
    return total_h


def _make_draw_header(invoice_date=''):
    """Return a canvas callback bound to the given invoice_date, since
    SimpleDocTemplate's onFirstPage/onLaterPages only accept (canvas, doc)."""
    def _draw_header(pdf_canvas, doc):
        pdf_canvas.saveState()
        y = PAGE_H - MARGIN
        for f in _build_header_flowables(invoice_date):
            w, h = f.wrap(CONTENT_W, y)
            y -= h
            f.drawOn(pdf_canvas, MARGIN, y)
        pdf_canvas.restoreState()
    return _draw_header


def build_proforma_pdf(proforma, customer, items):
    """Build proforma invoice PDF using Platypus framework."""
    buf = io.BytesIO()

    invoice_date = proforma.get('date', '')

    # Measure the header first so the body content never overlaps it,
    # regardless of font/content changes.
    header_height = _measure_header_height(invoice_date)
    top_margin = MARGIN + header_height + 0.15 * inch  # small gap below header

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=top_margin,
        bottomMargin=MARGIN
    )

    # Define styles
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

    # Order No. line — date left blank for manual fill-in when not provided
    order_no = proforma.get('order_no', '')
    order_date = proforma.get('order_date', '')
    order_line = (
        f"<b>Order No. ____{order_no}____</b>"
    )
    if order_date:
        order_line += (
            " " * 12
            + f"<b>Order Date ____{order_date}____</b>"
        )
    else:
        order_line += (
            " " * 50
            + "<b>Dated ____________</b>"
        )
    story.append(Paragraph(order_line, style_bold))
    story.append(Spacer(1, 0.1 * inch))

    if customer:
        company_name = customer.get('company_name', '').upper()
        buyer_line = f"<b><u>M/s.___ {company_name}___</u></b>"
        story.append(Paragraph(buyer_line, style_customer_name))
        story.append(Spacer(1, 0.05 * inch))

        address = _rl_multiline(customer.get('address', '').upper())
        addr_line = f"<b><u>__________{address}__________________</u></b>"
        story.append(Paragraph(addr_line, style_center_bold))
        story.append(Spacer(1, 0.15 * inch))

    # Line items table
    table_data = [['S.No.', 'Particulars', 'Qnty.', 'Rate (Rs.)', 'Amount (Rs.)']]

    subtotal = 0
    for idx, item in enumerate(items, 1):
        qty = item.get('quantity', 0)
        rate = item.get('rate', 0)
        amt = qty * rate
        subtotal += amt

        item_name = _rl_multiline(item.get('item_name', ''))
        description = _rl_multiline(item.get('description', ''))

        particulars_html = f"<b>{item_name}</b>"
        if description:
            particulars_html += f"<br/><i>{description}</i>"

        particulars = Paragraph(particulars_html, ParagraphStyle(
            'ItemStyle', parent=styles['Normal'],
            fontName='Helvetica', fontSize=9, leading=11
        ))

        qty_text = f"{int(qty)} Pcs." if qty == int(qty) else f"{_indian_format(qty, 0)} Kgs."

        row = [
            str(idx),
            particulars,
            qty_text,
            f"@ Rs. {_indian_format(rate)}",
            f"{_indian_format(amt)}"
        ]
        table_data.append(row)

    gst_amt = subtotal * 0.18
    total_amt = subtotal + gst_amt
    round_off = round(total_amt) - total_amt
    grand_total = round(total_amt)

    totals_rows = [
        ('Net Amount:', f"Rs. {_indian_format(subtotal)}"),
        ('Add: IGST @ 18%:', f"Rs. {_indian_format(gst_amt)}"),
        ('Total Amount:', f"Rs. {_indian_format(total_amt)}"),
        ('Round Off:', f"{round_off:.2f}"),
        ('Total Amount Payable:', f"Rs. {_indian_format(grand_total)}")
    ]

    for label, value in totals_rows:
        table_data.append(['', label, '', '', value])

    col_widths = [0.52 * inch, 3.49 * inch, 0.79 * inch, 1.08 * inch, 1.39 * inch]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)

    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#D9D9D9')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, black),
        ('ALIGN', (0, 1), (0, len(items)), 'CENTER'),
        ('ALIGN', (2, 1), (2, len(items)), 'CENTER'),
        ('ALIGN', (3, 1), (3, len(items)), 'CENTER'),
        ('ALIGN', (4, 1), (4, len(items)), 'RIGHT'),
        ('FONTNAME', (0, 1), (-1, len(items)), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, len(items)), 9),
    ]

    totals_start_row = len(items) + 1
    for i, (label, value) in enumerate(totals_rows):
        row_idx = totals_start_row + i
        table_style.append(('SPAN', (1, row_idx), (3, row_idx)))
        table_style.append(('ALIGN', (1, row_idx), (3, row_idx), 'RIGHT'))
        table_style.append(('ALIGN', (4, row_idx), (4, row_idx), 'RIGHT'))
        table_style.append(('FONTNAME', (1, row_idx), (-1, row_idx), 'Helvetica-Bold'))
        table_style.append(('FONTSIZE', (1, row_idx), (-1, row_idx), 10))
        if i == len(totals_rows) - 1:
            table_style.append(('TEXTCOLOR', (4, row_idx), (4, row_idx), HexColor('#FF0000')))

    items_table.setStyle(TableStyle(table_style))
    story.append(items_table)
    story.append(Spacer(1, 0.15 * inch))

    style_payment = ParagraphStyle(
        'PaymentBold', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=13, leading=16
    )
    footer_payment = f"Request to Send Payment of Rs. {_indian_format(grand_total, 0)}-00 via RTGS"
    story.append(Paragraph(footer_payment, style_payment))
    story.append(Spacer(1, 0.1 * inch))

    style_bank_intro = ParagraphStyle(
        'BankIntro', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, leading=13
    )
    # ── NOTES (printed if present; wrap words in **double-asterisks** to
    #    bold/highlight just that part) ──
    notes_text = proforma.get('notes', '')
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

    story.append(Paragraph("Our bank details are as follows:", style_bank_intro))
    story.append(Spacer(1, 0.08 * inch))

    # Bank details block
    style_bank_label = ParagraphStyle(
        'BankLabel', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=10, leading=14
    )
    style_bank_value = ParagraphStyle(
        'BankValue', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, leading=14
    )
    bank_rows = [
        ('Name', 'Aypee Industries'),
        ('Bank', 'Axis Bank Ltd.'),
        ('Account No.', '918030025772107'),
        ('IFSC', 'UTIB0000835'),
        ('Branch', 'Maqsudan (Jalandhar)'),
    ]
    bank_table_data = [
        [Paragraph(f"<b>{label}</b>", style_bank_label),
         Paragraph(f"-  {value}", style_bank_value)]
        for label, value in bank_rows
    ]
    bank_table = Table(bank_table_data, colWidths=[1.3 * inch, 3.5 * inch])
    bank_table.hAlign = 'LEFT'
    bank_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    story.append(bank_table)
    story.append(Spacer(1, 0.15 * inch))

    story.append(Paragraph("Subject to Jalandhar Jurisdiction", style_normal))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("<b>For AYPEE INDUSTRIES</b>", style_bold))
    story.append(Spacer(1, 0.15 * inch))

    signature_right = ParagraphStyle('SignatureRight', parent=style_bold, alignment=TA_RIGHT)
    story.append(Paragraph("<b>Raman Budhia (Partner)</b>", signature_right))
    story.append(Paragraph("<b>+91-9357255758</b>", signature_right))

    draw_header = _make_draw_header(invoice_date)
    doc.build(story, onFirstPage=draw_header, onLaterPages=draw_header)

    buf.seek(0)
    return buf.getvalue()
