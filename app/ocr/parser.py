"""
Turns extracted document lines (app/ocr/engine.py) into structured data:
who the customer is, the document's reference/date/subject, and the line
items (description, quantity, unit, rate).

This is *heuristic*. Enquiries and purchase orders come in endless layouts,
and OCR adds its own slips, so nothing here is guaranteed — the review page
exists for a person to check and fix the result before anything is saved or
quoted. The parser's job is to get most of it right, and to flag rows it
isn't sure of rather than hide the doubt.

Two ways of finding line items
------------------------------
1. Table mode — find the header row ("S.No | Description | Qty | Unit |
   Rate | Amount", in any order/wording), map every following row's cells
   to those columns (by position on the page, or by cell index for Word
   tables), and stitch wrapped multi-line descriptions back together.
2. Free-form mode — no table: pick out lines like
   "1. Sugar Elevator Chain 66.6 mm pitch - 120 Mtrs".

Our own company's details must never be mistaken for the customer's (an
enquiry is addressed *to* us): see OWN_* below.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from .engine import LOW_CONFIDENCE, Line

# ── Our own identity — never to be picked as "the customer" ──────────
# Keep in step with the letterhead in app/generators/pdf_generator.py.
OWN_KEYWORDS = ['aypee'] + [k.strip().lower() for k in
                            os.environ.get('OCR_OWN_KEYWORDS', '').split(',') if k.strip()]
OWN_GSTINS = {'03AAHFA4106G1Z4'}
OWN_EMAILS = {'aypeeinds@yahoo.com'}
OWN_PHONES = {'01812672260', '9357255758', '9041881996'}   # digits only, last 10 for mobiles


# ── Result types ─────────────────────────────────────────────────────
@dataclass
class ParsedItem:
    description: str                    # as read; wrapped lines joined by '\n'
    quantity: Optional[float] = None
    unit: str = ''
    rate: Optional[float] = None
    amount: Optional[float] = None
    conf: Optional[float] = None        # min OCR confidence of its lines
    flags: List[str] = field(default_factory=list)
    raw: str = ''                       # the source text, for the reviewer
    lines: List[Line] = field(default_factory=list, repr=False)   # source lines


@dataclass
class ParsedDocument:
    company_name: str = ''
    contact_person: str = ''
    address: str = ''
    phone: str = ''
    email: str = ''
    gstin: str = ''
    reference: str = ''
    date: str = ''                      # 'DD - MM - YYYY', the app's format
    subject: str = ''
    items: List[ParsedItem] = field(default_factory=list)
    mode: str = 'none'                  # table | freeform | none
    warnings: List[str] = field(default_factory=list)


# ── Small helpers ────────────────────────────────────────────────────
def _norm_space(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def _has_letters(s, n=2):
    return len(re.findall(r'[A-Za-z]', s or '')) >= n


def _is_own(text):
    """True if the text names our own company (tolerating OCR slips)."""
    low = (text or '').lower()
    if any(k in low for k in OWN_KEYWORDS):
        return True
    for word in re.findall(r'[a-z]{4,8}', low):
        for k in OWN_KEYWORDS:
            if len(k) >= 5 and SequenceMatcher(None, word, k).ratio() >= 0.8:
                return True
    return False


_NUM_TOKEN = re.compile(r'^[\d,]*\.?\d+$|^\d[\d,]*\.?$')


def parse_number(text):
    """'1,20,000.50' / 'Rs. 2700/-' / '1O0' -> float, or None."""
    if text is None:
        return None
    t = str(text).strip()
    t = re.sub(r'(?i)(?:rs\.?|inr|₹)', '', t)
    t = t.replace('/-', '').replace(' ', '').strip('=:*()[]|@')
    if not t:
        return None
    # OCR reads 0 as O inside an otherwise numeric token
    if any(ch.isdigit() for ch in t) and re.fullmatch(r'[\dOo,.]+', t):
        t = t.replace('O', '0').replace('o', '0')
    t = t.replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?|\.\d+', t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


_UNITS = {
    'mtr': 'Mtr.', 'mtrs': 'Mtr.', 'meter': 'Mtr.', 'meters': 'Mtr.', 'metre': 'Mtr.',
    'metres': 'Mtr.', 'rmtr': 'Mtr.', 'rmt': 'Mtr.',
    'nos': 'Nos.', 'no': 'Nos.', 'pcs': 'Pcs.', 'pc': 'Pcs.', 'piece': 'Pcs.', 'pieces': 'Pcs.',
    'kg': 'Kg.', 'kgs': 'Kg.', 'kilogram': 'Kg.', 'kilograms': 'Kg.',
    'set': 'Set', 'sets': 'Set', 'box': 'Box', 'boxes': 'Box',
    'ltr': 'Ltr.', 'ltrs': 'Ltr.', 'litre': 'Ltr.', 'litres': 'Ltr.', 'liter': 'Ltr.', 'liters': 'Ltr.',
    'pair': 'Pair', 'pairs': 'Pair', 'roll': 'Roll', 'rolls': 'Roll', 'bag': 'Bag', 'bags': 'Bag',
    'ton': 'Ton', 'tons': 'Ton', 'tonne': 'Ton', 'tonnes': 'Ton', 'ft': 'Ft.', 'feet': 'Ft.',
}
_UNIT_ALT = '|'.join(sorted(_UNITS, key=len, reverse=True))


def canon_unit(text):
    """'MTRS.' -> 'Mtr.'  (None when it isn't a recognised selling unit;
    'mm'/'cm' are dimensions, deliberately not units)."""
    return _UNITS.get(re.sub(r'[^a-z]', '', (text or '').lower()))


# ── Header fields ────────────────────────────────────────────────────
_GSTIN_DIGIT_POS = {0, 1, 7, 8, 9, 10}
_GSTIN_LETTER_POS = {2, 3, 4, 5, 6, 11}
_TO_DIGIT = {'O': '0', 'Q': '0', 'D': '0', 'I': '1', 'L': '1', 'S': '5', 'B': '8', 'Z': '2', 'G': '6'}
_TO_LETTER = {'0': 'O', '1': 'I', '5': 'S', '8': 'B', '2': 'Z', '6': 'G'}
_GSTIN_RE = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')


def _fix_gstin(s):
    """Correct the O/0, I/1, S/5 ... confusions OCR makes, using the fact
    that a GSTIN's letters and digits sit in fixed positions."""
    s = s.upper()
    out = []
    for i, ch in enumerate(s):
        if i in _GSTIN_DIGIT_POS:
            ch = _TO_DIGIT.get(ch, ch)
        elif i in _GSTIN_LETTER_POS:
            ch = _TO_LETTER.get(ch, ch)
        elif i == 13 and ch == '2':
            ch = 'Z'
        out.append(ch)
    return ''.join(out)


def _valid_gstin(g):
    return bool(_GSTIN_RE.match(g)) and 1 <= int(g[:2]) <= 38


def _is_own_gstin(g):
    return any(SequenceMatcher(None, g, own).ratio() >= 0.85 for own in OWN_GSTINS)


def find_gstin(texts):
    for t in texts:
        up = t.upper()
        if 'GST' in up:
            # slide a 15-character window over the label-and-number run, so
            # the label's own letters ("GSTNO...") can't hide the number
            squeezed = re.sub(r'[\s\-]', '', up)
            pool = re.findall(r'(?=([A-Z0-9]{15}))', squeezed)
        else:
            pool = re.findall(r'\b[A-Z0-9]{15}\b', up)
        for cand in pool:
            g = _fix_gstin(cand)
            if _valid_gstin(g) and not _is_own_gstin(g):
                return g
    return ''


def find_email(texts):
    for t in texts:
        t2 = re.sub(r'\s*@\s*', '@', t)              # OCR: "name @ site.com"
        for m in re.finditer(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}', t2):
            e = m.group(0).lower().strip('.')
            if e not in OWN_EMAILS and not _is_own(e):
                return e
    return ''


_PHONE_LABEL = re.compile(
    r'(?i)\b(?:ph(?:one)?|tel(?:ephone)?|mob(?:ile)?|cell|contact(?:\s*no)?|fax|off(?:ice)?|whatsapp)'
    r'\.?\s*(?:no\.?)?\s*[:.\-]*\s*\(?(?:off|res|mob)?\.?\)?\s*[:.\-]*\s*(\+?\d[\d\s\-/(),.]{6,})')


def _phone_digits(s):
    d = re.sub(r'\D', '', s)
    return d[-10:] if len(d) >= 10 and d[-10:][0] in '6789' else d


def find_phone(texts):
    found = []
    for t in texts:
        for m in _PHONE_LABEL.finditer(t):
            for part in re.split(r'[,/;]', m.group(1)):
                part = part.strip(' .-()')
                digits = re.sub(r'\D', '', part)
                if len(digits) < 8 or len(digits) > 13:
                    continue
                if _phone_digits(part) in OWN_PHONES or digits in OWN_PHONES:
                    continue
                found.append(_norm_space(part))
    return ', '.join(found[:2])


_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
_DATE_NUMERIC = re.compile(r'\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})\b')
# Fallback for when a lone '-' separator got dropped as OCR/table noise
# upstream (see _NOISE_ONLY in engine.py), leaving only "21 09 2026". A
# full 4-digit year is required here to keep this from firing on unrelated
# number runs.
_DATE_NUMERIC_LOOSE = re.compile(r'\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b')
_DATE_DMY = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})\b')
_DATE_MDY = re.compile(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')


def _fmt_date(d, m, y):
    y = int(y)
    if y < 100:
        y += 2000
    d, m = int(d), int(m)
    if 1 <= d <= 31 and 1 <= m <= 12 and 1990 <= y <= 2100:
        return f'{d:02d} - {m:02d} - {y}'
    return ''


def parse_date(text):
    """Find a date in free text and return it as 'DD - MM - YYYY' (day-first,
    as Indian documents are written) or ''."""
    m = _DATE_NUMERIC.search(text)
    if m:
        return _fmt_date(m.group(1), m.group(2), m.group(3))
    m = _DATE_DMY.search(text)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _fmt_date(m.group(1), _MONTHS[m.group(2)[:3].lower()], m.group(3))
    m = _DATE_MDY.search(text)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return _fmt_date(m.group(2), _MONTHS[m.group(1)[:3].lower()], m.group(3))
    m = _DATE_NUMERIC_LOOSE.search(text)
    if m:
        return _fmt_date(m.group(1), m.group(2), m.group(3))
    return ''


def find_date(texts):
    for t in texts:                                   # labelled dates first
        m = re.search(r'(?i)\b(?:date[d]?|dt)\b\s*[:.\-]?\s*(.+)', t)
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    for t in texts:
        d = parse_date(t)
        if d:
            return d
    return ''


_REF_LABELED = re.compile(
    r'(?i)\b(?:enquiry|inquiry|rfq|rfp|quotation|quote|p\.?\s?o\.?|purchase\s+order|order|indent|'
    r'ref(?:erence)?|your\s+ref)\s*(?:no\.?|number|#)\s*[:.\-]?\s*([A-Za-z0-9][A-Za-z0-9/_.\-]{2,39})')
_REF_COLON = re.compile(
    r'(?i)\b(?:enquiry|inquiry|rfq|rfp|p\.?\s?o\.?|purchase\s+order|indent|ref(?:erence)?|your\s+ref)'
    r'\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9/_.\-]{2,39})')


def find_reference(texts):
    for t in texts:
        for rx in (_REF_LABELED, _REF_COLON):
            for m in rx.finditer(t):
                v = m.group(1).strip('-.,:;/')
                if re.search(r'\d', v):
                    return v
    return ''


_SUBJECT_PREFIX = re.compile(
    r'(?i)^\s*(?:(?:request\s+for\s+)?(?:quotation|quote|enquiry|inquiry|rfq|offer|purchase\s+order|'
    r'price\s+quotation)\s*(?:for|of|regarding|on|[:\-])\s*)+')


def clean_subject(s):
    """'Enquiry for Chains' -> 'Chains'. The quotation PDF already prints
    'Quotation for <subject>', so a subject that repeats it would read
    'Quotation for Enquiry for Chains'."""
    s = _norm_space(s).strip(' .:-')
    s = _SUBJECT_PREFIX.sub('', s).strip(' .:-')
    return s[:1].upper() + s[1:] if s else ''


def find_subject(texts):
    for t in texts:
        m = re.match(r'(?i)^\s*(?:sub(?:ject)?|re|regarding)\s*[:.\-]+\s*(.+)$', t)
        if m:
            return clean_subject(m.group(1))
    return ''


# ── Customer block (name / contact / address) ────────────────────────
_COMPANY_WORDS = re.compile(
    r'(?i)\b(?:ltd|limited|pvt|private|llp|inc|corp|corporation|co|company|industries|industry|mills?|'
    r'enterprises?|traders?|trading|works|engineering|engineers|sugars?|distilleries|distillery|agro|'
    r'foods?|fabricators?|steels?|co-?operative|sahkari|udyog|factory|refiner(?:y|ies)|power|chemicals?|'
    r'projects?|infra|group|associates|exports?|impex|brothers|bros)\b\.?')
_MARKER = re.compile(
    r'(?i)^(?:(?P<to>to|from|bill(?:ed)?\s*to|ship(?:ped)?\s*to|buyer|customer|purchaser|consignee|sold\s*to)'
    r'\s*[:.,\-]*|(?P<ms>m\s?/\s?s\.?|messrs\.?))\s*(?P<rest>.*)$')
_STOP_BLOCK = re.compile(
    r'(?i)^(?:sub(?:ject)?|ref(?:erence)?|date[d]?|dear|respected|sir|madam|kind\s*attn|attn|attention|'
    r'enquiry|inquiry|po\b|p\.o|order|page|quot|yours|thank|please|kindly|we\b)')
_FIELD_LINE = re.compile(
    r'(?i)^(?:gst|ph(?:one)?|tel|mob|cell|e-?mail|website|web|fax|contact|pan|cin|www)\b|@|\bgstin\b')
_DOC_TITLES = re.compile(
    r'(?i)^(?:purchase\s+order|enquiry|inquiry|quotation|request\s+for\s+quotation|rfq|tax\s+invoice|'
    r'proforma|delivery\s+challan|indent|order\s+confirmation)\b')
_HONORIFIC = re.compile(r'(?i)^(?:mr|mrs|ms|miss|shri|shree|smt|sri|dr|er|prof)\b\.?\s+')
_PIN = re.compile(r'\b\d{6}\b')


def _is_company_line(t):
    return bool(_COMPANY_WORDS.search(t)) or bool(re.match(r'(?i)^m\s?/\s?s\b', t))


def _strip_ms(t):
    return re.sub(r'(?i)^\s*m\s?/\s?s\.?\s*', '', t).strip(' ,')


def _tidy_address_line(t):
    t = _norm_space(t).strip(' ,')
    return re.sub(r'(?<=[A-Za-z])\s+(\d{6})\s*$', r' - \1', t)        # "Ludhiana 141010"


def _split_block(block):
    """block: raw lines following a To:/M-s marker. -> (company, contact, address)."""
    block = [b for b in block if b]
    if not block:
        return '', '', ''
    ci = next((i for i, b in enumerate(block) if _is_company_line(b)), 0)
    company = _strip_ms(block[ci])
    contact = ''
    before = [b for b in block[:ci] if len(b.split()) <= 5 and not _PIN.search(b)]
    if before:
        contact = re.sub(r'(?i)^the\s+', '', before[-1]).strip(' ,')
    addr = []
    for b in block[ci + 1:]:
        if _FIELD_LINE.search(b):
            continue
        addr.append(_tidy_address_line(b))
        if _PIN.search(b) or len(addr) >= 4:
            break
    return company, contact, '\n'.join(addr)


def _collect_block(plain, start, first_text):
    block = [first_text] if first_text else []
    j = start + 1
    while j < len(plain) and len(block) < 7:
        t = plain[j]
        if _STOP_BLOCK.match(t):
            break
        if not _FIELD_LINE.search(t):
            block.append(t)
        j += 1
    return block, j


def find_customer(head):
    """-> (company, contact, address, came_from_to_block)"""
    # 1) explicit To:/M/s/Buyer: blocks — the first one that isn't us
    i = 0
    while i < len(head):
        m = _MARKER.match(head[i])
        if m:
            block, nxt = _collect_block(head, i, m.group('rest').strip())
            company, contact, address = _split_block(block)
            if company and not _is_own(company) and not _is_own(' '.join(block[:3])):
                return company, contact, address, True
            i = nxt
            continue
        i += 1

    # 2) letterhead: first company-looking line near the top that isn't us
    for i, t in enumerate(head[:14]):
        if _DOC_TITLES.match(t) or _FIELD_LINE.search(t) or _is_own(t):
            continue
        if _COMPANY_WORDS.search(t) and len(t.split()) <= 9:
            company = re.sub(r'(?i)^(?:buyer|customer|purchaser)\s*[:\-]\s*', '', _strip_ms(t))
            addr = []
            for b in head[i + 1:i + 5]:
                if _STOP_BLOCK.match(b) or _MARKER.match(b) or _DOC_TITLES.match(b) or _is_own(b):
                    break
                if _FIELD_LINE.search(b):
                    continue
                addr.append(_tidy_address_line(b))
                if _PIN.search(b) or len(addr) >= 3:
                    break
            return company, '', '\n'.join(addr), False
    return '', '', '', False


def find_contact(head, tail, from_to_block):
    for t in head:
        m = re.match(r'(?i)^\s*(?:contact\s*(?:person|name)?|contact)\s*[:.\-]+\s*(.+)$', t)
        if m and not re.search(r'\d{5,}', m.group(1)):
            return _norm_space(m.group(1))
        if from_to_block:                             # "Kind Attn" names the *recipient*; only
            m = re.match(r'(?i)^\s*(?:kind\s*attn|attn|attention)\s*[:.\-]+\s*(.+)$', t)  # meaningful if
            if m:                                     # the To: block was the customer
                return _norm_space(m.group(1)).strip(' .')
    # signatory after "Yours faithfully / Thanking you"
    for i, t in enumerate(tail):
        if re.match(r'(?i)^(?:yours\s+\w+|thanking\s+you|thanks|regards|with\s+regards|best\s+regards)\b', t):
            for cand in tail[i + 1:i + 5]:
                if re.match(r'(?i)^for\b', cand) or _is_company_line(cand) or re.search(r'\d', cand):
                    continue
                if _HONORIFIC.match(cand) or (
                        2 <= len(cand.split()) <= 4 and all(w[:1].isupper() for w in cand.split() if w[:1].isalpha())):
                    return _norm_space(cand).strip(' .,')
                break
    return ''


# ── Line items ───────────────────────────────────────────────────────
_ROLE_TESTS = [
    ('sno',   re.compile(r'(?i)^(?:s\.?\s*no\.?|sr\.?(?:\s*no\.?)?|sl\.?(?:\s*no\.?)?|serial(?:\s*no\.?)?|#|s\.?n\.?|no\.?)$')),
    ('skip',  re.compile(r'(?i)\b(?:hsn|sac|gst|tax|cgst|sgst|igst|disc(?:ount)?|code|remarks?|make|brand|delivery|schedule)\b')),
    ('qty',   re.compile(r'(?i)\b(?:qty|quantity|qnty|nos)\b')),
    ('rate',  re.compile(r'(?i)\b(?:rate|price|basic)\b')),
    ('amount', re.compile(r'(?i)\b(?:amount|amt|total|value)\b')),
    ('unit',  re.compile(r'(?i)^(?:units?|uom|u\.?o\.?m\.?|unit\s*of\s*measure)$')),
    ('desc',  re.compile(r'(?i)\b(?:desc\w*|particulars?|item|material|product|goods|article|details|name|specification)\b')),
]


def _role_of(text):
    t = _norm_space(text).strip(':.')
    if not t or len(t.split()) > 6:
        return None
    for role, rx in _ROLE_TESTS:
        if rx.search(t):
            return role
    return None


@dataclass
class _Header:
    idx: int
    cols: List[Tuple[str, float, float]]   # (role, x0, x1); geometry mode
    roles: List[str]                       # ordered roles; sequence mode
    group: Optional[int]


def _find_header(lines, start=0):
    for i in range(start, len(lines)):
        ln = lines[i]
        cells = [c for c in ln.cells if c.text]
        if 2 <= len(cells) <= 10:
            tagged = [(_role_of(c.text), c) for c in cells]
            hit = [(r, c) for r, c in tagged if r]
            roles = {r for r, _ in hit}
            if len(hit) >= 3 and len(hit) >= 0.6 * len(cells) and 'desc' in roles \
                    and roles & {'qty', 'rate', 'amount', 'unit'}:
                return _Header(i, [(r, c.x0, c.x1) for r, c in tagged if r] if len(hit) == len(cells)
                               else [(r or 'skip', c.x0, c.x1) for r, c in tagged],
                               [r for r, _ in hit], ln.group)
        elif len(cells) == 1 and len(cells[0].text.split()) >= 3:
            # header merged into one cell — sequence mode
            toks = [_role_of(w) for w in cells[0].text.split()]
            got = [r for r in toks if r]
            if len(got) >= 3 and 'desc' in got and set(got) & {'qty', 'rate', 'amount'} \
                    and len(got) >= 0.6 * len([t for t in cells[0].text.split() if len(t) > 2]):
                dedup = [r for k, r in enumerate(got) if k == 0 or r != got[k - 1]]
                return _Header(i, [], dedup, ln.group)
    return None


_STRONG_END = re.compile(
    r'(?i)^(?:sub[\s\-]*total|grand\s*total|total|net\s*(?:amount|total|payable)|amount\s+in\s+words|'
    r'rupees|in\s+words|thanking|thanks|regards|yours|authori[sz]ed|bank\s+details|e\.?\s*&\s*o\.?\s*e|'
    r'declaration|round(?:ing)?[\s\-]*off)\b')
_WEAK_END = re.compile(
    r'(?i)^(?:terms|note|notes|payment|delivery|gst|cgst|sgst|igst|tax|freight|packing|for\s+m/?s|'
    r'signature|subject\s+to|validity|warranty)\b')


def _assign(line, header):
    """Map a row's cells onto the header's columns -> {role: text}."""
    fields: Dict[str, List[str]] = {}
    cells = line.cells
    cols = header.cols

    if cols:
        if len(cells) == len(cols):                        # same shape: by index
            pairs = [(cols[k][0], c) for k, c in enumerate(cells)]
        else:                                              # by position on the page
            pairs = []
            for c in (c for c in cells if c.text):
                best, best_ov = None, 0.0
                for role, x0, x1 in cols:
                    ov = min(c.x1, x1) - max(c.x0, x0)
                    if ov > best_ov:
                        best, best_ov = role, ov
                if best is None and _has_letters(c.text) and any(r == 'desc' for r, _, _ in cols):
                    # A lettered cell that overlaps no column is a wrapped
                    # description/continuation line sitting at its own left
                    # margin (not aligned under the header's own word) —
                    # nearest-centre guessing was dumping these into
                    # whatever numeric column happened to be closest,
                    # losing the text entirely.
                    best = 'desc'
                if best is None:                           # no overlap: nearest column centre
                    cx = (c.x0 + c.x1) / 2
                    best = min(cols, key=lambda col: abs((col[1] + col[2]) / 2 - cx))[0]
                pairs.append((best, c))
        for role, c in pairs:
            if c.text:
                fields.setdefault(role, []).append(c.text)
        return {r: _norm_space(' '.join(v)) for r, v in fields.items()}

    # sequence mode: no column geometry — work token by token
    toks = line.plain.split()
    out: Dict[str, str] = {}
    if toks and 'sno' in header.roles and re.fullmatch(r'[\[(]?\d{1,3}[.)\]]?', toks[0]):
        out['sno'] = re.sub(r'\D', '', toks[0])
        toks = toks[1:]
    unit_i = next((i for i, t in enumerate(toks) if canon_unit(t) and i > 0), None)
    nums_after = []
    if unit_i is not None:
        out['unit'] = toks[unit_i]
        if parse_number(toks[unit_i - 1]) is not None:
            out['qty'] = toks[unit_i - 1]
            desc, nums_after = toks[:unit_i - 1], toks[unit_i + 1:]
        else:
            desc, nums_after = toks[:unit_i], toks[unit_i + 1:]
    else:
        k = len(toks)
        while k > 1 and parse_number(toks[k - 1]) is not None and len(toks) - k < 3:
            k -= 1
        desc, tail = toks[:k], toks[k:]
        order = [r for r in header.roles if r in ('qty', 'rate', 'amount')]
        for r, t in zip(order, tail):
            out[r] = t
    rest_roles = [r for r in header.roles if r in ('rate', 'amount')]
    for r, t in zip(rest_roles, [t for t in nums_after if parse_number(t) is not None]):
        out[r] = t
    out['desc'] = ' '.join(desc)
    return {k: v for k, v in out.items() if v}


def _qty_and_unit(qty_text, unit_text):
    """A quantity cell may hold '120', '120 Mtrs' or 'Qty 120'."""
    unit = canon_unit(unit_text) if unit_text else None
    q = None
    if qty_text:
        m = re.match(r'(?i)^\s*(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z.]*)\s*$', qty_text)
        if m:
            q = parse_number(m.group(1))
            unit = unit or canon_unit(m.group(2))
        else:
            q = parse_number(qty_text)
    return q, (unit or (unit_text.strip() if unit_text and not canon_unit(unit_text) and len(unit_text) <= 8 else ''))


def _strip_serial(text):
    return re.sub(r'^\s*[\[(]?\d{1,3}\s*[.)\]:\-]\s+', '', text)


@dataclass
class _Entry:
    line: Line
    f: Dict[str, str]
    kind: str            # item | text | nums | junk
    qty: Optional[float] = None
    unit: str = ''
    rate: Optional[float] = None
    amount: Optional[float] = None


def _sno_ok(f):
    """True only when the 'sno' cell is actually a small serial number —
    not just any truthy text that got column-matched into that slot."""
    v = f.get('sno', '')
    return bool(v) and bool(re.fullmatch(r'\d{1,3}', re.sub(r'[^\d]', '', v)))


def _classify(line, f):
    desc = f.get('desc', '')
    qty, unit = _qty_and_unit(f.get('qty', ''), f.get('unit', ''))
    rate, amount = parse_number(f.get('rate', '')), parse_number(f.get('amount', ''))
    sno_ok = _sno_ok(f)
    letters = _has_letters(desc)
    # A row only counts as a genuine new item/data row when a column holds a
    # value that actually PARSED as a number (qty/rate/amount) or a valid
    # serial. Raw text that merely landed in the qty/rate/unit column by
    # nearest-position guesswork (e.g. "per Mtr." at the tail of a wrapped
    # description) must not be enough to promote a continuation line to its
    # own item — that was producing duplicate/phantom rows.
    if letters and (qty is not None or rate is not None or sno_ok):
        kind = 'item'
    elif not letters and (qty is not None or rate is not None or amount is not None or sno_ok):
        kind = 'nums'
    elif letters:
        kind = 'text'
    else:
        kind = 'junk'
    return _Entry(line, f, kind, qty, unit, rate, amount)


def _gap(above, below):
    """Vertical gap between two lines on the same page, or None."""
    if above is None or below is None or above.page != below.page \
            or above.y1 is None or below.y0 is None:
        return None
    return below.y0 - above.y1


def _parse_table(lines, header):
    # Pass 1: assign every row's cells to columns, stopping at the totals.
    entries: List[_Entry] = []
    hdr = header
    for ln in lines[header.idx + 1:]:
        if not ln.plain:
            continue
        again = _find_header([ln], 0)                      # header repeated on the next page
        if again:
            hdr = again
            continue
        if hdr.group is not None and ln.group != hdr.group:  # a different Word table
            break
        f = _assign(ln, hdr)
        first = next((c.text for c in ln.cells if c.text), '')
        e = _classify(ln, f)
        numeric_start = e.qty is not None or _sno_ok(f)
        if _STRONG_END.match(first) or _STRONG_END.match(f.get('desc', '')):
            break
        if (_WEAK_END.match(first) or _WEAK_END.match(f.get('desc', ''))) and not numeric_start:
            break
        entries.append(e)

    # Pass 2: stitch rows into items. Wrapped descriptions are the hard
    # part: depending on the table's vertical alignment the first lines of a
    # description can sit ABOVE the row holding the numbers, and further
    # lines BELOW it. A run of stray text lines between two rows is split
    # at its largest vertical gap — lines before the gap trail the row
    # above, lines after it lead the row below.
    items: List[ParsedItem] = []
    cur: Optional[ParsedItem] = None
    have_rows = any(e.kind in ('item', 'nums') for e in entries)

    def begin(entry, lead_texts, lead_lines):
        nonlocal cur
        texts = lead_texts + ([entry.f['desc']] if entry.f.get('desc') else [])
        cur = ParsedItem(description='\n'.join(texts), quantity=entry.qty, unit=entry.unit,
                         rate=entry.rate, amount=entry.amount, lines=lead_lines + [entry.line])
        items.append(cur)

    i = 0
    while i < len(entries):
        e = entries[i]
        if e.kind == 'text' and have_rows:
            j = i
            while j < len(entries) and entries[j].kind == 'text':
                j += 1
            run = entries[i:j]
            nxt = entries[j] if j < len(entries) and entries[j].kind in ('item', 'nums') else None

            if nxt is None:
                split = len(run)                            # nothing below: all trail
            elif cur is None:
                split = 0                                   # nothing above: all lead
            else:
                chain = [cur.lines[-1]] + [r.line for r in run] + [nxt.line]
                gaps = [_gap(chain[k], chain[k + 1]) for k in range(len(chain) - 1)]
                split = max(range(len(gaps)), key=lambda k: gaps[k]) \
                    if all(g is not None for g in gaps) else len(run)   # no geometry: trail

            for r in run[:split]:
                if cur is not None:
                    cur.description += '\n' + r.f['desc']
                    cur.lines.append(r.line)
            leads = run[split:]
            if nxt is not None and (nxt.kind == 'item' or nxt.qty is not None or leads):
                begin(nxt, [r.f['desc'] for r in leads], [r.line for r in leads])
                i = j + 1
            else:
                i = j
            continue

        if e.kind in ('text', 'item'):                      # 'text' here = table with no numbered rows
            begin(e, [], [])
        elif e.kind == 'nums':
            if cur is not None and cur.quantity is None and e.qty is not None:
                cur.quantity, cur.unit = e.qty, e.unit or cur.unit
                cur.rate = e.rate if e.rate is not None else cur.rate
                cur.amount = e.amount if e.amount is not None else cur.amount
                cur.lines.append(e.line)
            elif e.qty is not None:
                begin(e, [], [])
        i += 1

    out = []
    for it in items:
        it.description = '\n'.join(
            _strip_serial(_norm_space(l)) for l in it.description.split('\n') if _norm_space(l))
        if not _has_letters(it.description):
            continue
        it.raw = ' / '.join(l.plain for l in it.lines)
        confs = [l.conf for l in it.lines if l.conf is not None]
        it.conf = min(confs) if confs else None
        out.append(it)
    return out


# ── Free-form items ──────────────────────────────────────────────────
_QTY_UNIT = re.compile(r'(?i)(?<![\d\w])(?<!\d\.)(\d[\d,]*(?:\.\d+)?)\s*(' + _UNIT_ALT + r')\b\.?')
_QTY_LABELLED = re.compile(r'(?i)\bqty\.?\s*[:\-]?\s*(\d[\d,]*(?:\.\d+)?)\s*(' + _UNIT_ALT + r')?\b\.?')
_RATE_INLINE = re.compile(
    r'(?i)(?:@|\brate\b\s*[:\-]?|\bat\b)\s*(?:rs\.?|₹|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/-)?'
    r'(?:\s*(?:per|/)\s*(?:' + _UNIT_ALT + r'))?')
_NOT_ITEM = re.compile(
    r'(?i)\b(?:dear|sir|madam|thank|regards|yours|please|kindly|quote|quotation|enquiry|inquiry|'
    r'subject|gstin|phone|mobile|email|www\.|pin\s*code|delivery|payment|terms|validity)\b')


_NUMBERED = re.compile(r'^\s*[\[(]?\d{1,3}\s*[.)\]:]\s+\S')


def _parse_freeform(lines):
    found: List[Tuple[int, ParsedItem]] = []
    used = set()
    prev_idx = None        # a description-only line waiting for a quantity line
    for idx, ln in enumerate(lines):
        t = ln.plain
        if not t:
            continue
        m = None
        for mm in _QTY_UNIT.finditer(t):
            m = mm                                                   # the last qty+unit on the line
        lab = _QTY_LABELLED.search(t)
        if lab and (m is None or lab.start() >= m.start()):
            m = lab
        if not m:
            prev_idx = idx if (_has_letters(t, 4) and not _NOT_ITEM.search(t)
                               and len(t) < 110 and not _FIELD_LINE.search(t)) else None
            continue

        qty, unit = parse_number(m.group(1)), canon_unit(m.group(2) or '') or ''
        rest = (t[:m.start()] + ' ' + t[m.end():]).strip()
        rate = None
        rm = _RATE_INLINE.search(rest)
        if rm:
            rate = parse_number(rm.group(1))
            rest = (rest[:rm.start()] + ' ' + rest[rm.end():]).strip()
        rest = re.sub(r'(?i)\b(?:qty|quantity|required|req)\b\.?', ' ', rest)
        numbered = bool(_NUMBERED.match(rest))
        desc = _strip_serial(rest)
        desc = _norm_space(re.sub(r'[\s\-–—:,;@.]+$', '', re.sub(r'^[\s\-–—:,;]+', '', desc)))

        src_idx = [idx]
        if not _has_letters(desc, 3) and prev_idx is not None:       # "120 Mtrs" under its description
            desc, src_idx = _strip_serial(_norm_space(lines[prev_idx].plain)), [prev_idx, idx]
        if not _has_letters(desc, 3) or _NOT_ITEM.search(desc) or _FIELD_LINE.search(desc):
            prev_idx = None
            continue
        if len([w for w in desc.split() if len(w) >= 3]) < 2 and not numbered:
            prev_idx = None
            continue
        src = [lines[k] for k in src_idx]
        confs = [l.conf for l in src if l.conf is not None]
        found.append((src_idx[0], ParsedItem(
            description=desc, quantity=qty, unit=unit, rate=rate, lines=src,
            raw=' / '.join(l.plain for l in src), conf=min(confs) if confs else None,
            flags=['Read from free text — check this row'])))
        used.update(src_idx)
        prev_idx = None

    # Safety net: inside a numbered list, a numbered line we couldn't read a
    # quantity from is still an item — most likely one whose quantity OCR
    # mangled. Surface it flagged rather than letting it silently vanish.
    if found:
        lo, hi = min(i for i, _ in found), max(i for i, _ in found)
        for idx in range(lo, hi + 1):
            t = lines[idx].plain
            if idx in used or not _NUMBERED.match(t):
                continue
            desc = _norm_space(re.sub(r'[\s\-–—:,;@.]+$', '', _strip_serial(t)))
            if _has_letters(desc, 4) and not _NOT_ITEM.search(desc) and not _FIELD_LINE.search(desc):
                found.append((idx, ParsedItem(
                    description=desc, lines=[lines[idx]], raw=t, conf=lines[idx].conf,
                    flags=['Read from free text — check this row'])))
    found.sort(key=lambda p: p[0])
    return [it for _, it in found]


# ── Post-processing shared by both modes ─────────────────────────────
def _post(items):
    for it in items:
        if it.quantity is None or it.quantity <= 0:
            it.flags.append('No quantity found')
        if it.quantity and it.rate and it.amount:
            expected = it.quantity * it.rate
            if abs(expected - it.amount) > max(1.0, 0.01 * it.amount):
                it.flags.append('Qty × rate does not equal the amount — check the figures')
        elif it.quantity and it.amount and not it.rate:
            it.rate = round(it.amount / it.quantity, 2)
            it.flags.append('Rate worked out from amount ÷ quantity')
        if it.conf is not None and it.conf < LOW_CONFIDENCE:
            it.flags.append('Poorly scanned — check this row')
    return items


# ── Entry point ──────────────────────────────────────────────────────
def parse(lines: List[Line]) -> ParsedDocument:
    lines = [ln for ln in lines if ln.plain]
    doc = ParsedDocument()
    if not lines:
        doc.warnings.append('Nothing to read.')
        return doc

    header = _find_header(lines)
    items: List[ParsedItem] = []
    end_of_head = header.idx if header else len(lines)

    if header:
        items = _parse_table(lines, header)
        doc.mode = 'table'
    if not items:
        items = _parse_freeform(lines)
        if items:
            doc.mode = 'freeform'
            first_item_raw = items[0].raw.split(' / ')[0]
            end_of_head = next((k for k, ln in enumerate(lines) if ln.plain == first_item_raw), len(lines))
    doc.items = _post(items)

    head = [ln.plain for ln in lines[:end_of_head]]
    head_no_own = [t for t in head if not _is_own(t)]
    tail = [ln.plain for ln in lines[end_of_head:]]
    everything = [ln.plain for ln in lines]

    company, contact, address, from_to = find_customer(head)
    doc.company_name, doc.address = company, address
    doc.contact_person = find_contact(head, tail, from_to) or contact
    doc.gstin = find_gstin(head) or find_gstin(everything)
    doc.email = find_email(head_no_own)
    doc.phone = find_phone(head_no_own)
    doc.date = find_date(head)
    doc.reference = find_reference(head)
    doc.subject = find_subject(head)

    if not doc.items:
        doc.warnings.append('No line items were recognised. Add them by hand below.')
    if not doc.company_name:
        doc.warnings.append('The customer name could not be identified — please fill it in.')
    return doc
