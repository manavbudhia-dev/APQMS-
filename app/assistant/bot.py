"""
One chat message in -> one reply out. Shared by the Telegram bot, the
/api/v1/chat endpoint and `apqms_cli.py chat`.

Replies are plain text (no Markdown, so nothing needs escaping for
Telegram) plus, where it makes sense, a PDF and the structured document.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from app.assistant import core, sheet_bot, tokens
from app.assistant.core import PROFORMA, QUOTATION, AssistantError
from app.assistant.parser import parse
from app.generators.pdf_generator import _indian_format

HELP = """APQMS Bot — kya kar sakta hoon:

1) Quotation banana:
quotation Krishna Sugar Mills
10 mtr roller chain 25.4 @ 450
bearing 6205 x 5
subject: Spares for season

2) Proforma banana (same format):
proforma Krishna Sugar: 10 mtr chain 25.4 @450, 5 nos bearing 6205
po: 4512

3) Quotation se proforma:
AQ-2026-012 ka proforma bana do   (ya: isko proforma bana do)

4) PDF / details / status:
AQ-2026-012 pdf  |  AQ-2026-012  |  AQ-2026-012 status sent / confirm / cancel

5) List & lookup:
quotations  |  recent proforma  |  confirmed quotations
customer krishna  |  item chain 25.4  |  summary

6) Sheet ka text (phone camera / Google Lens se copy karke yahan paste karein):
bot values padhega, aap check karke likhein:  ok Krishna Sugar Mills
galti ho to:  rate 8360  |  delivery 8  |  cancel

7) Naya customer:
add customer Shree Ram Sugars
contact: Ramesh   phone: 98xxxxxxx   gstin: 09XXXXX

Tips: rate na likhein to catalog rate lagega. Unit: mtr, pcs, kg, nos, set...
Nayi documents hamesha Draft banti hain — app mein check karke Sent karein."""


@dataclass
class Reply:
    text: str
    pdf: bytes | None = None
    pdf_name: str | None = None
    data: dict = field(default_factory=dict)
    ok: bool = True


def rs(v):
    return 'Rs. ' + _indian_format(v or 0)


def _q(v):
    return core._fmt_num(v)


def format_doc(doc, headline=None):
    label = 'Quotation' if doc['type'] == QUOTATION else 'Proforma Invoice'
    lines = [headline or f"{label} {doc.get('number') or '(preview)'}"]
    cust = doc.get('customer') or {}
    lines.append(f"Customer: {cust.get('company_name') or '-'}")
    meta = [f"Status: {doc.get('status')}", f"Date: {doc.get('date')}"]
    if doc.get('order_no'):
        meta.append(f"PO: {doc['order_no']}")
    lines.append(' | '.join(meta))
    if doc.get('subject'):
        lines.append(f"Subject: {doc['subject']}")
    lines.append('')
    for i, it in enumerate(doc.get('items') or [], 1):
        amt = (it.get('quantity') or 0) * (it.get('rate') or 0)
        flag = ''
        if 'from_catalog' in it and not it['from_catalog']:
            flag = '  [catalog mein nahi — free text]'
        lines.append(f"{i}. {it['item_name']} — {_q(it['quantity'])} {it.get('unit') or ''} x "
                     f"{rs(it['rate'])} = {rs(amt)}{flag}")
    t = doc.get('totals') or core.totals(doc.get('items') or [])
    lines.append('')
    lines.append(f"Subtotal {rs(t['subtotal'])} | GST {core.GST_PERCENT}% {rs(t['gst'])}")
    lines.append(f"Grand Total: {rs(t['grand_total'])}")
    return '\n'.join(lines)


def _resolve_ref(intent, chat_key, want_kind=None):
    """Document number from the message, else the chat's last document."""
    ref = intent.get('ref')
    kind = intent.get('kind') or core.kind_from_number(ref)
    if ref:
        return kind or want_kind or QUOTATION, ref
    k, doc_id = tokens.recall(chat_key)
    if not doc_id:
        raise AssistantError('Kaunsa document? Number likhein, jaise AQ-2026-012.', 'ref_required')
    return k, str(doc_id)


def _with_pdf(doc, text, attach):
    if not attach:
        return Reply(text, data={'document': doc})
    pdf, name = core.render_pdf(doc['type'], doc['id'])
    return Reply(text, pdf=pdf, pdf_name=name, data={'document': doc})


def handle(text, user, chat_key=None, attach_pdf=True, notify=None):
    """user: dict with at least 'id'. chat_key: identifies the conversation
    for "isko / last" follow-ups (e.g. 'tg:12345'). notify(text), if given,
    sends an interim message (e.g. "reading the sheet…") before a slow step."""
    try:
        # Answer to "Kya aapka matlab: <customer>?" asked last time?
        r = _handle_customer_choice(text, user, chat_key, attach_pdf)
        if r is not None:
            return r
        # A sheet pasted earlier and still waiting for "ok" / corrections?
        r = sheet_bot.handle_pending(text, user, chat_key, attach_pdf)
        if r is not None:
            return r
        # Text copied off a hand-edited quotation sheet -> AI extraction.
        if sheet_bot.looks_like_sheet(text):
            return sheet_bot.start(text, user, chat_key, notify)
        return _handle(text, user, chat_key, attach_pdf)
    except AssistantError as e:
        return Reply('⚠️ ' + e.message, ok=False, data=e.to_dict())


_YES = {'yes', 'y', 'haan', 'han', 'ha', 'haa', 'haan ji', 'ji', 'ji haan', 'ok', 'okay', 'sahi', 'sahi hai',
        'correct', 'yes please', 'theek', 'theek hai', 'thik hai', 'done', 'confirm', 'yep', 'yup', 'hmm'}
_NO = {'no', 'n', 'nahi', 'nahin', 'na', 'cancel', 'chhodo', 'rehne do', 'stop'}


def _norm(t):
    return re.sub(r'[^a-z0-9 ]+', ' ', (t or '').lower()).split()


def _pick_customer(text, suggestions):
    """Which suggestion the reply means, or None."""
    low = ' '.join(_norm(text))
    if low in _YES and len(suggestions) == 1:
        return suggestions[0]
    m = re.fullmatch(r'(?:no\.?\s*)?(\d)', low)
    if m and 1 <= int(m.group(1)) <= len(suggestions):
        return suggestions[int(m.group(1)) - 1]
    words = _norm(text)
    if not words:
        return None
    hits = [sg for sg in suggestions if all(any(w2.startswith(w) for w2 in _norm(sg)) for w in words)]
    return hits[0] if len(hits) == 1 else None


def _handle_customer_choice(text, user, chat_key, attach_pdf):
    key = f'cust:{chat_key}' if chat_key else None
    p = tokens.get_pending(key)
    if not p:
        return None
    low = ' '.join(_norm(text))
    if low in _NO:
        tokens.set_pending(key, None)
        return Reply('Theek hai, chhod diya. Dobara likhein jab chahein.')
    choice = _pick_customer(text, p['suggestions'])
    if choice is None:
        if low in _YES:
            return Reply('Kaunsa customer? Number likhein: ' + ', '.join(
                f'{i}) {sg}' for i, sg in enumerate(p['suggestions'], 1)), ok=False)
        tokens.set_pending(key, None)          # something else: treat as a new message
        return None
    tokens.set_pending(key, None)
    args = dict(p['args'])
    args['customer'] = choice
    return _create(p['kind'], args, user, chat_key, attach_pdf)


def _create(kind, args, user, chat_key, attach_pdf):
    try:
        doc = core.create_document(kind, args, user['id'])
    except AssistantError as e:
        sugg = e.detail.get('suggestions') if e.code == 'customer_not_found' else None
        if sugg and chat_key:
            tokens.set_pending(f'cust:{chat_key}', {'kind': kind, 'args': args, 'suggestions': sugg[:5]})
            if len(sugg) == 1:
                msg = (f'Customer "{args.get("customer")}" clearly nahi mila. Kya aapka matlab: {sugg[0]}?\n'
                       '"haan" likhein to isi naam se ban jaayegi, ya "no".')
            else:
                msg = (f'Customer "{args.get("customer")}" clearly nahi mila. Kaunsa? Number likhein:\n'
                       + '\n'.join(f'{i}) {sg}' for i, sg in enumerate(sugg[:5], 1)))
            return Reply('⚠️ ' + msg, ok=False, data=e.to_dict())
        raise
    tokens.remember(chat_key, kind, doc['id'])
    label = 'Quotation' if kind == QUOTATION else 'Proforma'
    txt = format_doc(doc, f"✅ {label} {doc['number']} ban gayi ({doc['status']})")
    if kind == QUOTATION:
        txt += '\n\nProforma chahiye to likhein: isko proforma bana do'
    return _with_pdf(doc, txt, attach_pdf)


def _handle(text, user, chat_key, attach_pdf):
    intent = parse(text)
    name = intent['intent']

    if name == 'help':
        return Reply(HELP, data={'intent': 'help'})

    if name == 'unknown':
        return Reply('Samajh nahi aaya. "help" likhein — examples mil jayenge.', ok=False,
                     data={'intent': 'unknown'})

    if name == 'create':
        kind, args = intent['kind'], dict(intent['args'])
        if not args.get('customer'):
            raise AssistantError('Customer ka naam likhein, jaise: quotation Krishna Sugar Mills '
                                 '(agli lines mein items).', 'customer_required')
        if not args.get('items'):
            raise AssistantError('Items nahi mile. Har line mein ek item likhein, jaise: '
                                 '10 mtr roller chain 25.4 @ 450', 'invalid_items')
        return _create(kind, args, user, chat_key, attach_pdf)

    if name == 'convert':
        kind, ref = _resolve_ref(intent, chat_key, QUOTATION)
        if kind != QUOTATION:
            raise AssistantError('Proforma sirf quotation se banti hai — quotation number dein '
                                 '(AQ-...).', 'wrong_type')
        pi = core.quotation_to_proforma(ref, user['id'], order_no=intent.get('order_no') or '',
                                        mark_confirmed=intent.get('confirm', False))
        tokens.remember(chat_key, PROFORMA, pi['id'])
        txt = format_doc(pi, f"✅ Proforma {pi['number']} ban gayi (from {pi['source_quotation']})")
        return _with_pdf(pi, txt, attach_pdf)

    if name == 'pdf':
        kind, ref = _resolve_ref(intent, chat_key)
        doc = core.get_document(kind, ref)
        tokens.remember(chat_key, kind, doc['id'])
        pdf, fname = core.render_pdf(kind, doc['id'])
        return Reply(f"📄 {fname}", pdf=pdf, pdf_name=fname, data={'document': doc})

    if name == 'status':
        kind, ref = _resolve_ref(intent, chat_key)
        doc = core.set_status(kind, ref, intent['status'])
        tokens.remember(chat_key, kind, doc['id'])
        return Reply(f"✅ {doc['number']} status: {doc['status']}", data={'document': doc})

    if name == 'show':
        kind, ref = _resolve_ref(intent, chat_key)
        doc = core.get_document(kind, ref)
        tokens.remember(chat_key, kind, doc['id'])
        return Reply(format_doc(doc), data={'document': doc})

    if name == 'list':
        rows = core.list_documents(intent['kind'], q=intent.get('q', ''),
                                   status=intent.get('status', ''), limit=10)
        label = 'Quotations' if intent['kind'] == QUOTATION else 'Proforma Invoices'
        if not rows:
            return Reply(f'Koi {label.lower()} nahi mili.', data={'documents': []})
        out = [f"{label} (latest {len(rows)}):"]
        for r in rows:
            out.append(f"• {r['number']} | {r['customer'] or '-'} | {r['status']} | "
                       f"{rs(r['grand_total'])} | {r['date']}")
        return Reply('\n'.join(out), data={'documents': rows})

    if name == 'customers':
        rows = core.search_customers(intent.get('q', ''), limit=10)
        if not rows:
            return Reply('Koi customer nahi mila.', data={'customers': []})
        out = ['Customers:'] + [f"• {r['company_name']} (id {r['id']})"
                                + (f" — {r['phone']}" if r.get('phone') else '') for r in rows]
        return Reply('\n'.join(out), data={'customers': rows})

    if name == 'items':
        rows = core.search_items(intent.get('q', ''), limit=10)
        if not rows:
            return Reply('Koi item nahi mila.', data={'items': []})
        out = ['Items:'] + [f"• {r['item_name']} — {rs(r['rate'])} / {r['unit'] or '-'} (id {r['id']})"
                            for r in rows]
        return Reply('\n'.join(out), data={'items': rows})

    if name == 'add_customer':
        c = core.create_customer(intent['data'])
        return Reply(f"✅ Customer added: {c['company_name']} (id {c['id']})", data={'customer': c})

    if name == 'summary':
        s = core.summary()
        out = ['📊 Summary']
        for kind, label in ((QUOTATION, 'Quotations'), (PROFORMA, 'Proforma')):
            k = s[kind]
            out.append(f"\n{label} — is mahine: {k['this_month']['count']} ({rs(k['this_month']['value'])} + GST)")
            for st, v in k['by_status'].items():
                out.append(f"  {st}: {v['count']} ({rs(v['value'])})")
        out.append(f"\nCustomers: {s['customers']} | Items: {s['items']}")
        return Reply('\n'.join(out), data={'summary': s})

    return Reply('Samajh nahi aaya. "help" likhein.', ok=False)
