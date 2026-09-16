"""Generate packing list PDF using ReportLab Platypus framework."""
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


def _build_header_flowables(pl_date=''):
    """Build the header content as a list of flowables for packing list."""
    flowables = []

    # GSTIN line
    gstin_style = ParagraphStyle('gstin', fontName='Helvetica-Bold', fontSize=12,
                                  alignment=TA_LEFT, leading=14)
    flowables.append(Paragraph("GSTIN: 03AAHFA4106G1Z4", gstin_style))
    flowables.append(Spacer(1, 4))

    title_style = ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=16,
                                  alignment=TA_CENTER, leading=19)
    flowables.append(Paragraph("PACKING LIST", title_style))
    flowables.append(Spacer(1, 2))

    company_style = ParagraphStyle('company', fontName='Helvetica-Bold', fontSize=30,
                                    alignment=TA_CENTER, leading=34,
                                    textColor=HexColor('#FF0000'))
    flowables.append(Paragraph("AYPEE INDUSTRIES", company_style))
    flowables.append(Spacer(1, 6))

    # --- 3-column address / CHAINS / contact table ---
    addr_style = ParagraphStyle('addr', fontName='Helvetica', fontSize=11, leading=13)
    addr_html = (
        "<b><u>Correspondence Address:</u></b><br/>"
        "STREET NO.1, ANAND NAGAR<br/>"
        "NEAR CHADHA ENGG. WORKS,<br/>"
        "MAKSUDAN, JALANDHAR<br/>"
        "(PUNJAB), INDIA<br/>"
        "PIN CODE &#8211; 144008"
    )
    addr_para = Paragraph(addr_html, addr_style)

    chains_style = ParagraphStyle('chains', fontName='Helvetica-Bold', fontSize=18,
                                   textColor=HexColor('#FF0000'), alignment=TA_CENTER)
    chains_para = Paragraph("CHAINS", chains_style)

    contact_style = ParagraphStyle('contact', fontName='Helvetica', fontSize=12, leading=15)
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

    mfg_style = ParagraphStyle('mfg', fontName='Helvetica-Bold', fontSize=12,
                                alignment=TA_CENTER, leading=14)
    flowables.append(Paragraph(
        "MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, "
        "RBC & RAKE CARRIER CHAINS, SLAT & SPARE PARTS",
        mfg_style
    ))
    flowables.append(Spacer(1, 3))

    email_style = ParagraphStyle('email', fontName='Helvetica', fontSize=12,
                                  alignment=TA_CENTER)
    email_para = Paragraph(
        'E-mail: <u><font color="#0000FF">aypeeinds@yahoo.com</font></u>',
        email_style
    )

    date_field_style = ParagraphStyle('date_field', fontName='Helvetica-Bold', fontSize=12,
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

    styles = getSampleStyleSheet()

    style_normal = ParagraphStyle(
        'CustomNormal', parent=styles['Normal'],
        fontName='Helvetica', fontSize=12, leading=15
    )
    style_bold = ParagraphStyle(
        'CustomBold', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=12, leading=15
    )
    style_center = ParagraphStyle('CustomCenter', parent=style_normal, alignment=TA_CENTER)
    style_center_bold = ParagraphStyle('CustomCenterBold', parent=style_bold, alignment=TA_CENTER)
    style_customer_name = ParagraphStyle('CustomerName', parent=style_bold, alignment=TA_CENTER,
                                          fontSize=15, leading=18)

    story = []

    # Packing List No. line
    pl_no = packing_list.get('pl_no', '')
    order_line = (
        f"<b>Packing List No. ____{pl_no}____</b>"
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

    # Packing items table - with bag breakdown
    # For each item, show: S.No., Particulars, Total Qty, Unit, Qty/Bag, Total Bags, Bag Breakdown
    table_data = [['S.No.', 'Particulars', 'Total Qty', 'Unit', 'Qty/Bag', 'Bags', 'Bag Breakdown']]

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
            fontName='Helvetica', fontSize=11, leading=13
        ))

        unit = item.get('unit', 'Pcs')

        row = [
            str(idx),
            particulars,
            str(int(total_qty) if total_qty == int(total_qty) else total_qty),
            unit,
            str(int(bag_qty) if bag_qty == int(bag_qty) else bag_qty),
            str(total_bags),
            bag_breakdown
        ]
        table_data.append(row)

    col_widths = [0.4 * inch, 2.5 * inch, 0.7 * inch, 0.6 * inch, 0.65 * inch, 0.5 * inch, 2.4 * inch]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)

    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#D9D9D9')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, black),
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'CENTER'),
        ('ALIGN', (5, 1), (5, -1), 'CENTER'),
        ('ALIGN', (6, 1), (6, -1), 'CENTER'),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 11),
    ]

    items_table.setStyle(TableStyle(table_style))
    story.append(items_table)
    story.append(Spacer(1, 0.2 * inch))

    # Footer
    style_footer = ParagraphStyle(
        'Footer', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, leading=13
    )
    story.append(Paragraph("Subject to Jalandhar Jurisdiction", style_footer))
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