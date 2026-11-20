"""
One chat message in -> one reply out. Shared by the Telegram bot, the
/api/v1/chat endpoint and `apqms_cli.py chat`.

Replies are plain text (no Markdown, so nothing needs escaping for
Telegram) plus, where it makes sense, a PDF and the structured document.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.assistant import core, tokens
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

6) Naya customer:
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


def handle(text, user, chat_key=None, attach_pdf=True):
    """user: dict with at least 'id'. chat_key: identifies the conversation
    for "isko / last" follow-ups (e.g. 'tg:12345')."""
    try:
        return _handle(text, user, chat_key, attach_pdf)
    except AssistantError as e:
        return Reply('⚠️ ' + e.message, ok=False, data=e.to_dict())


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
        doc = core.create_document(kind, args, user['id'])
        tokens.remember(chat_key, kind, doc['id'])
        label = 'Quotation' if kind == QUOTATION else 'Proforma'
        txt = format_doc(doc, f"✅ {label} {doc['number']} ban gayi ({doc['status']})")
        if kind == QUOTATION:
            txt += '\n\nProforma chahiye to likhein: isko proforma bana do'
        return _with_pdf(doc, txt, attach_pdf)

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
