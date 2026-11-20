"""
Telegram / chat flow for a quotation sheet whose text the user copied on
their phone (camera "copy text", Google Lens) and pasted into the bot:

  1. user pastes the sheet text            -> Claude reads it, bot shows values
                                              (⚠️ doubtful ones, 🔴 rate/qty/delivery)
  2. user corrects if needed               -> "rate 8360", "delivery 8", "payment 25% advance..."
  3. user replies "ok Krishna Sugar"       -> quotation + PDF in the chat
     ("cancel" drops it)

Copied text has no strikethroughs, so when an old and a new value both
appear the bot shows both and the user decides. Every step is recorded in
vision_extractions (pasted text, AI output, edits, confirmed values).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.assistant import core, tokens
from app.vision import confirm, extractor, store
from app.vision.schema import empty_extraction

PENDING_HOURS = 24

# Words that appear in quotation-sheet terms but hardly ever in a normal
# bot command; 3+ of them means "this is a pasted sheet".
_SHEET_WORDS = ['gst', 'delivery', 'payment', 'advance', 'ex factory', 'ex-factory', 'weeks', 'pitch',
                'attachment', 'hardened', 'lock washer', 'moc', 'inquiry', 'enquiry', 'tempered',
                'forged', 'per mtr', 'per mt', 'total width', 'rates']

_OK_RE = re.compile(r'^\s*(?:ok|okay|okk|haan|han|ha|yes|confirm|confirmed|sahi(?:\s+hai)?|theek(?:\s+hai)?|'
                    r'thik(?:\s+hai)?|done)\b[\s,:-]*(.*)$', re.I | re.S)
_CANCEL_RE = re.compile(r'^\s*(?:cancel|radd|chhodo|chodo|rehne\s+do|discard)\b', re.I)
_SHOW_RE = re.compile(r'^\s*(?:show|dikhao|values|status)\s*$', re.I)
_CUSTOMER_RE = re.compile(r'^\s*(?:customer|party|client)\s*[:=\-]?\s*(.+)$', re.I)

# (pattern, field, numeric) — longest / most specific first.
_CORRECTIONS = [
    (r'original\s*rate|old\s*rate|purani\s*rate|printed\s*rate', 'original_rate_per_mtr', True),
    (r'(?:attachment|att)\s*height|height', 'attachment.height_mm', True),
    (r'(?:attachment|att)\s*thickness|thickness|thk', 'attachment.thickness_mm', True),
    (r'(?:attachment|att)\s*(?:material|moc)?|moc|material', 'attachment.material', False),
    (r'total\s*width|width', 'total_width_mm', True),
    (r'rate|bhav|price', 'rate_per_mtr', True),
    (r'qty|quantity|mtrs?|meters?|metres?', 'quantity_mtrs', True),
    (r'delivery|weeks?', 'delivery_weeks', True),
    (r'gst', 'gst_percent', True),
    (r'payment|pay', 'payment_terms', False),
    (r'price\s*basis|basis', 'price_basis', False),
    (r'product', 'product', False),
    (r'link', 'link_material', False),
    (r'pin', 'pin', False),
    (r'notes?|remark', 'notes', False),
]
_CORR_RE = [(re.compile(r'^\s*(?:' + p + r')\b\s*[:=\-]?\s*(.+?)\s*$', re.I), f, n) for p, f, n in _CORRECTIONS]


def _extract(text):
    """Seam for tests."""
    return extractor.extract_text(text)


def _key(chat_key):
    return f'{chat_key}|sheet'


def looks_like_sheet(text):
    t = (text or '').lower()
    if re.match(r'^\s*/?sheet\b', t):
        return True
    if len(t) < 60:
        return False
    return sum(1 for w in _SHEET_WORDS if w in t) >= 3


def _get(d, path):
    if '.' in path:
        a, b = path.split('.', 1)
        return (d.get(a) or {}).get(b)
    return d.get(path)


def _set(d, path, value):
    if '.' in path:
        a, b = path.split('.', 1)
        d.setdefault(a, {})[b] = value
    else:
        d[path] = value


def _n(v):
    if v is None:
        return '—'
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f'{v:,}' if isinstance(v, (int, float)) and v >= 1000 else str(v)


def _pending(chat_key):
    kind, rid = tokens.recall(_key(chat_key))
    if kind != 'sheet' or not rid:
        return None
    row = store.get(rid)
    if row is None or row['quotation_id'] or row['status'] not in (store.STATUS_EXTRACTED, store.STATUS_FAILED):
        return None
    try:
        created = datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S')
        if datetime.now(timezone.utc).replace(tzinfo=None) - created > timedelta(hours=PENDING_HOURS):
            return None
    except (TypeError, ValueError):
        pass
    return row


def _values(row):
    v = row['draft'] or row['extracted'] or empty_extraction()
    v.setdefault('attachment', {})
    return v


def summary(row, values, headline=None):
    from app.assistant.bot import rs
    flagged = {}
    corrected = set(values.get('_corrected') or [])
    for item in (row['extracted'] or {}).get('needs_review') or []:
        if item['field'] not in corrected:          # the user already fixed it
            flagged.setdefault(item['field'], []).append(item['reason'])

    def mark(path):
        return '⚠️ ' if path in flagged else ''

    att = values.get('attachment') or {}
    lines = [headline or f'📋 Sheet #{row["id"]} — ye values mili:', '']
    lines.append(f'{mark("product")}Product: {values.get("product") or "—"}')
    if values.get('link_material'):
        lines.append(f'{mark("link_material")}Link: {values["link_material"]}')
    if values.get('pin'):
        lines.append(f'{mark("pin")}Pin: {values["pin"]}')
    att_parts = [p for p in (att.get('material'),
                             f'H {_n(att["height_mm"])}mm' if att.get('height_mm') is not None else None,
                             f'T {_n(att["thickness_mm"])}mm' if att.get('thickness_mm') is not None else None) if p]
    if att_parts:
        m = '⚠️ ' if any(k.startswith('attachment') for k in flagged) else ''
        lines.append(f'{m}Attachment: ' + ', '.join(att_parts))
    if values.get('total_width_mm') is not None:
        lines.append(f'{mark("total_width_mm")}Total width: {_n(values["total_width_mm"])}mm')
    lines.append('')
    rate = values.get('rate_per_mtr')
    orig = values.get('original_rate_per_mtr')
    lines.append(f'🔴 {mark("quantity_mtrs")}Qty: {_n(values.get("quantity_mtrs"))} mtr')
    lines.append(f'🔴 {mark("rate_per_mtr")}Rate: {rs(rate) if rate is not None else "—"} / mtr'
                 + (f'   (purani: {rs(orig)})' if orig is not None else ''))
    lines.append(f'🔴 {mark("delivery_weeks")}Delivery: {_n(values.get("delivery_weeks"))} weeks')
    lines.append(f'{mark("gst_percent")}GST: {_n(values.get("gst_percent"))}% | '
                 f'{mark("price_basis")}{values.get("price_basis") or "—"}')
    lines.append(f'{mark("payment_terms")}Payment: {values.get("payment_terms") or "—"}')
    if values.get('notes'):
        lines.append(f'{mark("notes")}Note: {values["notes"]}')
    if rate and values.get('quantity_mtrs'):
        lines.append(f'Amount: {rs(rate * values["quantity_mtrs"])} + GST')
    if values.get('_customer_name'):
        lines.append(f'Customer: {values["_customer_name"]}')
    if flagged:
        lines += ['', '⚠️ Dhyan se check karein:']
        for f, reasons in flagged.items():
            for r in reasons:
                lines.append(f'• {f}: {r}')
    missing = confirm.check_values(values)
    lines.append('')
    if missing:
        lines.append('❗ ' + ' '.join(missing))
    lines.append('Galti ho to sudhaarein, jaise:  rate 8360  |  qty 70  |  delivery 8  |  payment 25% advance ...')
    lines.append('🔴 wali values sheet se milakar, sahi ho to likhein:  ok <customer naam>'
                 + ('' if not values.get('_customer_name') else '  (ya sirf: ok)'))
    lines.append('Chhodna ho to: cancel')
    return '\n'.join(lines)


def start(text, user, chat_key, notify=None):
    from app.assistant.bot import Reply
    body = re.sub(r'^\s*/?sheet\b[\s:]*', '', text, flags=re.I).strip()
    if notify:
        notify('📋 Sheet ka text mil gaya — padh raha hoon (20-40 sec)…')
    rid = store.create_text(user['id'], body)
    try:
        result = _extract(body)
    except extractor.ExtractionError as e:
        store.save_error(rid, e.message, e.raw_text)
        return Reply(f'⚠️ Sheet padh nahi paya: {e.message}', ok=False, data={'sheet_id': rid})
    store.save_result(rid, result)
    tokens.remember(_key(chat_key), 'sheet', rid)
    row = store.get(rid)
    return Reply(summary(row, _values(row)), data={'sheet_id': rid, 'values': _values(row)})


def _parse_corrections(text):
    """-> (changes {path: value}, customer or None, errors) or None if this
    message isn't a correction at all."""
    changes, customer, errors = {}, None, []
    for line in [l for l in text.splitlines() if l.strip()]:
        cm = _CUSTOMER_RE.match(line)
        if cm:
            customer = cm.group(1).strip()
            continue
        if re.match(r'^\s*ex[\s-]*factory', line, re.I):
            changes['price_basis'] = line.strip()
            continue
        for rx, path, numeric in _CORR_RE:
            m = rx.match(line)
            if not m:
                continue
            val = m.group(1).strip()
            if numeric:
                n, _ = extractor._to_number(val)
                if n is None:
                    return None             # e.g. "rate of grease" — a normal bot command
                changes[path] = n
            else:
                changes[path] = val[:500]
            break
        else:
            return None
    return (changes, customer, errors) if (changes or customer) else None


def handle_pending(text, user, chat_key, attach_pdf=True):
    """Reply for a message that belongs to a pending sheet, else None."""
    from app.assistant.bot import Reply, format_doc
    row = _pending(chat_key)
    if row is None:
        return None
    values = _values(row)

    if _CANCEL_RE.match(text):
        tokens.remember(_key(chat_key), 'sheet', None)
        return Reply(f'❌ Sheet #{row["id"]} chhod di. Nayi sheet ka text kabhi bhi paste karein.')
    if _SHOW_RE.match(text):
        return Reply(summary(row, values))

    m = _OK_RE.match(text)
    if m:
        name = m.group(1).strip()
        cid = values.get('_customer_id')
        if name:
            try:
                c = core.resolve_customer(name)
            except core.AssistantError as e:
                return Reply('⚠️ ' + e.message + '\nPhir likhein: ok <poora customer naam>', ok=False)
            cid = c['id']
            values['_customer_id'], values['_customer_name'] = c['id'], c['company_name']
            store.save_draft(row['id'], values)
        if not cid:
            return Reply('Customer kaun hai? Likhein:  ok Krishna Sugar Mills', ok=False)
        clean = {k: v for k, v in values.items() if not k.startswith('_')}
        try:
            doc = confirm.create_quotation(row['id'], clean, cid, user['id'], source='Telegram sheet')
        except confirm.ConfirmError as e:
            return Reply('⚠️ Quotation nahi bani: ' + ' '.join(e.errors), ok=False)
        tokens.remember(_key(chat_key), 'sheet', None)
        tokens.remember(chat_key, core.QUOTATION, doc['id'])
        txt = format_doc(doc, f"✅ Quotation {doc['number']} ban gayi (Draft)")
        txt += '\n\nProforma chahiye to likhein: isko proforma bana do'
        if not attach_pdf:
            return Reply(txt, data={'document': doc})
        pdf, name = core.render_pdf(core.QUOTATION, doc['id'])
        return Reply(txt, pdf=pdf, pdf_name=name, data={'document': doc})

    parsed = _parse_corrections(text)
    if parsed is None:
        return None
    changes, customer, _ = parsed
    for path, val in changes.items():
        _set(values, path, val)
        values.setdefault('_corrected', [])
        if path not in values['_corrected']:
            values['_corrected'].append(path)
    note = []
    if customer:
        try:
            c = core.resolve_customer(customer)
            values['_customer_id'], values['_customer_name'] = c['id'], c['company_name']
        except core.AssistantError as e:
            note.append('⚠️ ' + e.message)
    store.save_draft(row['id'], values)
    head = '✏️ Update ho gaya. Ab values:'
    out = summary(row, values, head)
    if note:
        out = '\n'.join(note) + '\n\n' + out
    return Reply(out, data={'sheet_id': row['id'], 'values': values})

