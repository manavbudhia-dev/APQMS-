"""
Free-text chat message -> intent + arguments. Rule based, no network, no
dependencies; understands simple English / Hinglish. Examples:

    quotation Krishna Sugar Mills
    10 mtr roller chain 25.4 @ 450
    bearing 6205 x 5 @ 250
    grease 5 kg
    subject: Spares for season

    proforma for Krishna Sugar: 10 mtr chain 25.4 @450, 5 nos bearing 6205
    AQ-2026-012 ka proforma bana do          (convert quotation -> proforma)
    isko proforma bana do / pdf bhejo        (acts on the last document)
    AQ-2026-012 pdf      AQ-2026-012 status sent      AQ-2026-012
    quotations / recent proforma / confirmed quotations
    customer krishna     item chain 25.4      summary      help
    add customer Krishna Sugar Mills
    phone: 98xxxxxxx
"""
from __future__ import annotations

import re

from app.assistant.core import DOC_NO_RE, PROFORMA, QUOTATION, kind_from_number

_UNITS = [
    (r'mtrs?|meters?|metres?|mtr|m', 'Mtr.'),
    (r'pcs|pc|pieces?', 'Pcs.'),
    (r'kgs?|kilos?', 'Kg.'),
    (r'nos|no|numbers?|nag', 'Nos.'),
    (r'sets?', 'Set'),
    (r'ltrs?|litres?|liters?|l', 'Ltr.'),
    (r'pairs?', 'Pair'),
    (r'rolls?', 'Roll'),
    (r'bags?', 'Bag'),
    (r'tons?|tonnes?', 'Ton'),
    (r'ft|feet|foot', 'Ft.'),
    (r'box|boxes', 'Box'),
]
UNIT_RE = '(?:' + '|'.join(p for p, _ in _UNITS) + r')\.?'
_NUM = r'\d[\d,]*(?:\.\d+)?'


def norm_unit(u):
    u = (u or '').strip().lower().rstrip('.')
    for pat, canon in _UNITS:
        if re.fullmatch(pat, u):
            return canon
    return ''


def _n(s):
    try:
        return float(str(s).replace(',', ''))
    except (TypeError, ValueError):
        return None


_KIND_WORDS = {
    QUOTATION: r'quotations?|quotes?|qtns?|qtn|kotation|quatation|qoutation',
    PROFORMA: r'proformas?|proforma\s+invoices?|performa|perfoma|p\.?i\.?|pis',
}
_KIND_RE = {k: re.compile(r'\b(?:' + v + r')\b', re.I) for k, v in _KIND_WORDS.items()}

_STATUS_WORDS = [
    (r'confirm(?:ed)?|approved?|pakka', 'Confirmed'),
    (r'cancel(?:led|ed)?|radd', 'Cancelled'),
    (r'sent|bhej\s*di(?:ya)?|bheja', 'Sent'),
    (r'draft', 'Draft'),
]

_FIELD_RE = re.compile(
    r'^\s*(customer|party|client|company|subject|sub|notes?|remarks?|order\s*no\.?|po\s*no\.?|po|'
    r'order\s*date|po\s*date|status|enquiry\s*no\.?|enquiry|enq\s*no\.?|ref|enquiry\s*date|date|'
    r'contact(?:\s*person)?|phone|mobile|email|gstin|gst|address)\s*[:=]\s*(.+?)\s*$', re.I)

_FIELD_KEYS = {
    'customer': 'customer', 'party': 'customer', 'client': 'customer', 'company': 'customer',
    'subject': 'subject', 'sub': 'subject', 'note': 'notes', 'notes': 'notes', 'remark': 'notes',
    'remarks': 'notes', 'order no': 'order_no', 'po no': 'order_no', 'po': 'order_no',
    'order date': 'order_date', 'po date': 'order_date', 'status': 'status',
    'enquiry no': 'enquiry_no', 'enquiry': 'enquiry_no', 'enq no': 'enquiry_no', 'ref': 'enquiry_no',
    'enquiry date': 'enquiry_date', 'date': 'date', 'contact': 'contact_person',
    'contact person': 'contact_person', 'phone': 'phone', 'mobile': 'phone', 'email': 'email',
    'gstin': 'gstin', 'gst': 'gstin', 'address': 'address',
}

_FILLER = re.compile(
    r'\b(?:new|create|make|generate|prepare|banao|banado|bana\s+do|bana\s+de|bana|banana|hai|do|de|'
    r'please|pls|plz|kindly|for|ke\s+liye|ka|ki|ke|ko|to|a|an|the|naya|nayi|ek|invoice|customer|'
    r'party|send|bhejo|chahiye|of)\b', re.I)

_CONTEXT_WORDS = re.compile(r'\b(?:isko|iska|iski|isse|ise|isey|last|pichla|pichli|previous|'
                            r'this|it|ye|yeh|usko|uska|same|wahi|wohi)\b', re.I)
_PDF_WORDS = re.compile(r'\b(?:pdf|bhejo|bhej|send|download|file|copy|print)\b', re.I)
_LIST_WORDS = re.compile(r'\b(?:list|recent|latest|last|sab|saari|sare|all|dikhao|show|aaj\s*ki|'
                         r'today)\b', re.I)


def _status_in(text):
    for pat, canon in _STATUS_WORDS:
        if re.search(r'\b(?:' + pat + r')\b', text, re.I):
            return canon
    return None


def _kind_in(text):
    """Which document kind a phrase mentions. Proforma wins when both
    appear ("quotation ka proforma"), since that is the target."""
    if _KIND_RE[PROFORMA].search(text):
        return PROFORMA
    if _KIND_RE[QUOTATION].search(text):
        return QUOTATION
    return None


def _doc_ref(text):
    m = DOC_NO_RE.search(text)
    return f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}" if m else None


# ── item lines ───────────────────────────────────────────────
_RATE_RE = re.compile(
    r'(?:@|₹|\brs\.?|\binr\b|\brate\s*[:=]?|\bbhav\s*[:=]?|\bprice\s*[:=]?)\s*(' + _NUM + r')\s*(?:/-)?'
    r'(?:\s*(?:per|/)\s*' + UNIT_RE + r')?', re.I)
_QTY_KW_RE = re.compile(r'\b(?:qty|quantity)\s*[:=]?\s*(' + _NUM + r')\s*(' + UNIT_RE + r')?(?![a-z])', re.I)
_QTY_X_TRAIL = re.compile(r'(?:^|\s)[x×*]\s*(' + _NUM + r')\s*(' + UNIT_RE + r')?(?![a-z0-9])', re.I)
_QTY_X_LEAD = re.compile(r'^(' + _NUM + r')\s*(' + UNIT_RE + r')?\s*[x×*]\s+', re.I)
_QTY_LEAD = re.compile(r'^(' + _NUM + r')\s*(' + UNIT_RE + r')?(?=\s)', re.I)
_QTY_TRAIL_UNIT = re.compile(r'\s(' + _NUM + r')\s*(' + UNIT_RE + r')\s*$', re.I)
_QTY_TRAIL_DASH = re.compile(r'\s[-–:=]\s*(' + _NUM + r')\s*(' + UNIT_RE + r')?\s*$', re.I)


def parse_item(line):
    """'10 mtr roller chain 25.4 @ 450' -> {item, quantity, unit, rate}."""
    s = ' ' + line.strip() + ' '
    out = {'item': '', 'quantity': None, 'unit': '', 'rate': None}
    m = _RATE_RE.search(s)
    if m:
        out['rate'] = _n(m.group(1))
        s = s[:m.start()] + ' ' + s[m.end():]
    s = re.sub(r'\s+', ' ', s).strip()
    for rx in (_QTY_KW_RE, _QTY_X_LEAD, _QTY_X_TRAIL, _QTY_LEAD, _QTY_TRAIL_UNIT, _QTY_TRAIL_DASH):
        m = rx.search(s)
        if m:
            out['quantity'] = _n(m.group(1))
            out['unit'] = norm_unit(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else ''
            s = (s[:m.start()] + ' ' + s[m.end():])
            break
    name = re.sub(r'\s+', ' ', s).strip(' -–:,;=*')
    out['item'] = name
    return out


def _split_items(chunks):
    items = []
    for chunk in chunks:
        for seg in re.split(r';|,(?=\s)|,(?=[A-Za-z])|\s+\+\s+', chunk):
            seg = re.sub(r'^\s*(?:[-*•▪➤>]+|\d{1,2}[.)])\s+', '', seg).strip()
            if seg:
                items.append(parse_item(seg))
    return items


# ── public ───────────────────────────────────────────────────
def parse(text):
    raw = (text or '').strip()
    low = re.sub(r'\s+', ' ', raw.lower())
    if not raw:
        return {'intent': 'help'}
    cmd = low.split(' ', 1)[0].split('@', 1)[0]          # "/help@MyBot"
    if cmd in ('/start', '/help', 'help', 'madad', 'menu', '?', 'hi', 'hello', 'namaste'):
        return {'intent': 'help'}
    if low in ('summary', 'report', 'dashboard', 'hisaab', 'hisab', '/summary', 'status report'):
        return {'intent': 'summary'}

    lines = [l for l in raw.splitlines() if l.strip()]
    first = lines[0]
    ref = _doc_ref(raw)

    # ── add customer ──
    m = re.match(r'^\s*/?(?:add\s+customer|new\s+customer|customer\s+add|naya\s+customer|'
                 r'add\s+party)\b[\s:,-]*(.*)$', first, re.I)
    if m:
        data, extra = {}, []
        for l in [m.group(1)] + lines[1:]:
            fm = _FIELD_RE.match(l)
            if fm:
                key = _FIELD_KEYS.get(re.sub(r'[.\s]+', ' ', fm.group(1).lower()).strip(), '')
                data['company_name' if key == 'customer' else key] = fm.group(2)
            elif l.strip():
                extra.append(l.strip())
        if not data.get('company_name') and extra:
            data['company_name'] = extra.pop(0)
        return {'intent': 'add_customer', 'data': data}

    # ── create a new document ──
    if not ref:
        header, rest = first, lines[1:]
        if ':' in first:
            h, after = first.split(':', 1)
            if not _FIELD_RE.match(first) and _kind_in(h):
                header, rest = h, ([after] if after.strip() else []) + rest
        kind = _kind_in(header)
        if kind and (rest or ':' in first) and not _CONTEXT_WORDS.search(header):
            fields, item_chunks = {}, []
            for l in rest:
                fm = _FIELD_RE.match(l)
                if fm:
                    fields[_FIELD_KEYS.get(re.sub(r'[.\s]+', ' ', fm.group(1).lower()).strip(),
                                           'notes')] = fm.group(2)
                else:
                    item_chunks.append(l)
            name = _KIND_RE[PROFORMA].sub(' ', header)
            name = _KIND_RE[QUOTATION].sub(' ', name)
            name = _FILLER.sub(' ', name)
            name = re.sub(r'\s+', ' ', name).strip(' -–:,.')
            customer = fields.pop('customer', '') or name
            st = fields.pop('status', None)
            args = {'customer': customer, 'items': _split_items(item_chunks)}
            if st:
                args['status'] = _status_in(st) or st
            args.update(fields)
            return {'intent': 'create', 'kind': kind, 'args': args}

    # ── act on an existing document (by number, or "isko"/last) ──
    uses_context = bool(_CONTEXT_WORDS.search(low)) or low in ('pdf', 'pdf bhejo', 'send pdf')
    target_kind = kind_from_number(ref) if ref else None
    if ref or uses_context:
        mentions = _kind_in(re.sub(DOC_NO_RE, ' ', low))
        order = re.search(r'\b(?:po|order)\s*(?:no\.?|number)?\s*[:=]?\s*([A-Za-z0-9/\-]+)', raw, re.I)
        if mentions == PROFORMA and target_kind != PROFORMA and not _PDF_WORDS.search(low.replace('proforma', '')):
            return {'intent': 'convert', 'ref': ref,
                    'order_no': order.group(1) if order and not DOC_NO_RE.match(order.group(1)) else '',
                    'confirm': bool(re.search(r'\bconfirm', low))}
        if _PDF_WORDS.search(low):
            return {'intent': 'pdf', 'ref': ref, 'kind': target_kind or mentions}
        st = _status_in(low)
        if st:
            return {'intent': 'status', 'ref': ref, 'kind': target_kind or mentions, 'status': st}
        if ref:
            return {'intent': 'show', 'ref': ref, 'kind': target_kind}

    # ── lists / lookups ──
    kind = _kind_in(low)
    if kind and (_LIST_WORDS.search(low) or re.fullmatch(r'/?\w+s|/?\w+', low) or _status_in(low)):
        q = _FILLER.sub(' ', _LIST_WORDS.sub(' ', _KIND_RE[kind].sub(' ', low)))
        st = _status_in(low)
        if st:
            for pat, _c in _STATUS_WORDS:
                q = re.sub(r'\b(?:' + pat + r')\b', ' ', q)
        return {'intent': 'list', 'kind': kind, 'status': st or '', 'q': re.sub(r'\s+', ' ', q).strip()}
    m = re.match(r'^/?(?:customers?|party|parties|clients?)\b\s*(.*)$', low)
    if m:
        return {'intent': 'customers', 'q': m.group(1).strip()}
    m = re.match(r'^/?(?:items?|products?|rate|rates|price|bhav)\b\s*(?:of|ka|ki)?\s*(.*)$', low)
    if m:
        return {'intent': 'items', 'q': m.group(1).strip()}
    if re.search(r'\b(?:summary|report|dashboard|hisaab|hisab)\b', low):
        return {'intent': 'summary'}
    return {'intent': 'unknown'}
