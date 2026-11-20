"""
Business operations for the assistant API / bot / CLI.

Everything here works on plain Python values and returns plain dicts, so the
same code backs the JSON API, the Telegram bot and the command line. Writes
reuse the exact helpers the web forms use (`_insert_quotation_row`,
`_save_quotation_items`, ...), so a quotation made from a phone is
indistinguishable from one made in the browser: same numbering, same
transaction handling, same line-item snapshot rules.
"""
from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher

from database import (begin_immediate, gen_proforma_no, gen_quotation_no, get_db)
from app.ocr.matching import CatalogIndex, _company_key, tokens
from app.generators.pdf_generator import build_pdf
from app.generators.proforma_generator import build_proforma_pdf
from app.views.quotation_pages import (QuotationNumberConflictError, QuotationPagesMixin,
                                       QuotationSaveError)
from app.views.proforma_pages import (ProformaNumberConflictError, ProformaPagesMixin,
                                      ProformaSaveError)
from utils.http_utils import FormData

DEFAULT_TERMS = ('1. Rates are Ex-works.\n2. GST 18% Extra as applicable.\n'
                 '3. Payment: 20% Advance and Balance against delivery.\n'
                 '4. Delivery: 10-12 weeks after receipt of order.')
GST_PERCENT = 18
DEFAULT_UNIT = 'Nos.'
CUSTOMER_MATCH = 0.82          # score needed to auto-pick a customer
MAX_ITEMS_PER_DOC = 200

QUOTATION = 'quotation'
PROFORMA = 'proforma'
STATUSES = {
    QUOTATION: list(QuotationPagesMixin.QUOTATION_STATUSES),
    PROFORMA: list(ProformaPagesMixin.PROFORMA_STATUSES),
}

# Per-kind table / column names. Never built from user input.
_META = {
    QUOTATION: dict(table='quotations', no='quotation_no', items='quotation_items',
                    fk='quotation_id', label='Quotation', prefix='AQ-'),
    PROFORMA: dict(table='proforma_invoices', no='pi_no', items='proforma_invoice_items',
                   fk='proforma_id', label='Proforma Invoice', prefix='API-'),
}

DOC_NO_RE = re.compile(r'\b(AQ|API)-(\d{4})-(\d{1,6})\b', re.IGNORECASE)


class AssistantError(Exception):
    """A request that can't be carried out, with a message fit to show the
    user (and optional structured detail for API clients)."""

    def __init__(self, message, code='bad_request', status=400, detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail or {}

    def to_dict(self):
        out = {'error': self.code, 'message': self.message}
        if self.detail:
            out['detail'] = self.detail
        return out


class _Ops(QuotationPagesMixin, ProformaPagesMixin):
    """The web-form mixins, used only for their request-independent save
    helpers (numbering + line-item insertion)."""


_ops = _Ops()


def _today():
    return datetime.now().strftime('%d - %m - %Y')


def _num(v):
    """Accept 10, 10.5, '1,000', ' 250 ' -> float, or None."""
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return QuotationPagesMixin._parse_numeric_field(str(v))


def _fmt_num(v):
    if v is None:
        return ''
    return ('%f' % float(v)).rstrip('0').rstrip('.') or '0'


def _clean(v, limit=500):
    return str(v or '').strip()[:limit]


def kind_of(value):
    """'quotation' / 'proforma' from loose words (q, qtn, pi, proforma...)."""
    v = str(value or '').strip().lower()
    if v in ('q', 'qt', 'qtn', 'quote', 'quotes', 'quotation', 'quotations'):
        return QUOTATION
    if v in ('p', 'pi', 'pis', 'proforma', 'proformas', 'proforma_invoice', 'proforma-invoice',
             'proforma_invoices', 'invoice', 'invoices'):
        return PROFORMA
    raise AssistantError(f'Unknown document type "{value}" — use quotation or proforma.')


# ─────────────────────────────────────────────────────────────
#  Customers
# ─────────────────────────────────────────────────────────────
def _customer_dict(r):
    return {k: r[k] for k in ('id', 'company_name', 'contact_person', 'address', 'phone',
                              'email', 'gstin') if k in r.keys()}


def _customer_score(query, row):
    qk = _company_key(query)
    if not qk:
        return 0.0
    best = 0.0
    for field in ('company_name', 'contact_person'):
        ck = _company_key(row[field])
        if not ck:
            continue
        if ck == qk:
            s = 1.0
        elif re.search(r'\b' + re.escape(qk) + r'\b', ck):
            s = 0.80 + 0.15 * (len(qk) / len(ck))      # "krishna" in "krishna sugar mills"
        else:
            s = SequenceMatcher(None, qk, ck).ratio()
            q_t, c_t = set(qk.split()), set(ck.split())
            if q_t and q_t <= c_t:
                s = max(s, 0.85)
        if field == 'contact_person':
            s *= 0.95
        best = max(best, s)
    return best


def search_customers(query='', limit=10):
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM customers WHERE COALESCE(is_active,1)=1 "
                          "ORDER BY company_name").fetchall()
    finally:
        db.close()
    query = _clean(query, 200)
    if not query:
        return [_customer_dict(r) for r in rows[:limit]]
    g = query.upper().replace(' ', '')
    exact = [r for r in rows if (r['gstin'] or '').upper().replace(' ', '') == g]
    if exact:
        return [dict(_customer_dict(r), score=1.0) for r in exact]
    scored = sorted(((_customer_score(query, r), r) for r in rows), key=lambda t: -t[0])
    return [dict(_customer_dict(r), score=round(s, 3)) for s, r in scored[:limit] if s >= 0.45]


def resolve_customer(ref):
    """Customer id / exact GSTIN / fuzzy name -> customer dict.

    Raises AssistantError listing the closest names when nothing is a clear
    winner, so a bot can ask "did you mean ...?" instead of guessing."""
    if isinstance(ref, dict):
        ref = ref.get('id') or ref.get('company_name') or ''
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        db = get_db()
        try:
            row = db.execute("SELECT * FROM customers WHERE id=?", (int(ref),)).fetchone()
        finally:
            db.close()
        if not row:
            raise AssistantError(f'No customer with id {ref}.', 'customer_not_found', 404)
        return _customer_dict(row)
    ref = _clean(ref, 200)
    if not ref:
        raise AssistantError('Customer name is required.', 'customer_required')
    # 1) every word of the query starts a word of exactly one customer's
    #    name ("krishna", "shree ram") -> that customer. Two or more such
    #    customers ("sugar") -> ambiguous, ask instead of guessing.
    qk = _company_key(ref).split()
    if qk:
        db = get_db()
        try:
            rows = db.execute("SELECT * FROM customers WHERE COALESCE(is_active,1)=1").fetchall()
        finally:
            db.close()
        exact = [r for r in rows if _company_key(r['company_name']) == ' '.join(qk)]
        if len(exact) == 1:
            return _customer_dict(exact[0])
        prefix = [r for r in rows
                  if all(any(w.startswith(q) for w in _company_key(r['company_name']).split()) for q in qk)]
        if len(prefix) == 1 and len(''.join(qk)) >= 3:
            return _customer_dict(prefix[0])
        if not prefix and len(''.join(qk)) >= 4:
            # Spacing differs: "wiper drive" vs "WIPERDRIVE ENGINEERING".
            joined = ''.join(qk)
            glued = [r for r in rows if _company_key(r['company_name']).replace(' ', '').startswith(joined)]
            if len(glued) == 1:
                return _customer_dict(glued[0])
        if len(prefix) > 1:
            names = [r['company_name'] for r in prefix[:5]]
            raise AssistantError(f'"{ref}" naam ke kai customer hain: ' + ', '.join(names)
                                 + '. Poora naam likhein.', 'customer_not_found', 404,
                                 {'query': ref, 'suggestions': names})
    # 2) fuzzy (typos: "krisna sugar")
    matches = search_customers(ref, limit=5)
    if matches:
        top = matches[0]
        second = matches[1]['score'] if len(matches) > 1 else 0
        if top['score'] >= 0.99 or (top['score'] >= CUSTOMER_MATCH and top['score'] - second >= 0.05):
            top.pop('score', None)
            return top
    suggestions = [m['company_name'] for m in matches[:5]]
    if suggestions:
        msg = f'Customer "{ref}" clearly nahi mila. Kya aapka matlab: ' + ', '.join(suggestions) + '?'
    else:
        msg = f'Customer "{ref}" nahi mila. Pehle customer add karein.'
    raise AssistantError(msg, 'customer_not_found', 404, {'query': ref, 'suggestions': suggestions})


def create_customer(data):
    company = _clean(data.get('company_name') or data.get('name'), 200)
    if not company:
        raise AssistantError('company_name is required.', 'company_required')
    contact = _clean(data.get('contact_person'), 200) or company
    db = get_db()
    try:
        begin_immediate(db)
        existing = db.execute("SELECT * FROM customers").fetchall()
        key = _company_key(company)
        for r in existing:
            if key and _company_key(r['company_name']) == key:
                db.rollback()
                raise AssistantError(f'Customer "{r["company_name"]}" already exists (id {r["id"]}).',
                                     'customer_exists', 409, {'customer': _customer_dict(r)})
        cid = db.execute(
            "INSERT INTO customers (contact_person, company_name, address, phone, email, gstin) "
            "VALUES (?,?,?,?,?,?)",
            (contact, company, _clean(data.get('address'), 1000), _clean(data.get('phone'), 50),
             _clean(data.get('email'), 200), _clean(data.get('gstin'), 20).upper())).lastrowid
        db.commit()
        row = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        return _customer_dict(row)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
#  Items (catalog)
# ─────────────────────────────────────────────────────────────
def _item_dict(r):
    return {'id': r['id'], 'item_name': r['item_name'], 'description': r['description'] or '',
            'specification': r['specification'] or '', 'unit': r['unit'] or '',
            'rate': r['rate_per_unit'] or 0}


def _catalog():
    db = get_db()
    try:
        return db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
    finally:
        db.close()


def search_items(query='', limit=10, _rows=None):
    rows = _rows if _rows is not None else _catalog()
    query = _clean(query, 300)
    if not query:
        return [_item_dict(r) for r in rows[:limit]]
    idx = CatalogIndex(rows)
    q = tokens(query)
    if not q:
        return []
    scored = sorted(((idx.score(q, i), i) for i in range(len(rows))), key=lambda t: -t[0])
    return [dict(_item_dict(rows[i]), score=round(s, 3)) for s, i in scored[:limit] if s >= 0.25]


def resolve_items(lines, _rows=None):
    """Turn requested lines into fully specified line items.

    Each line is a dict with any of: item_id, item / item_name (text to look
    up), description, specification, quantity, unit, rate. Catalog matches
    fill in name / description / spec / unit / rate; anything the caller
    gave explicitly wins. A line with no catalog match is still accepted
    as a free-text item as long as it has a rate.

    Returns (items, issues) — issues is a list of human-readable problems;
    the caller must not save when it is non-empty."""
    if not isinstance(lines, list) or not lines:
        return [], ['Kam se kam ek item chahiye (at least one item is required).']
    if len(lines) > MAX_ITEMS_PER_DOC:
        return [], [f'Too many items (max {MAX_ITEMS_PER_DOC}).']
    rows = _rows if _rows is not None else _catalog()
    by_id = {r['id']: r for r in rows}
    idx = CatalogIndex(rows) if rows else None
    out, issues = [], []
    for n, line in enumerate(lines, 1):
        if isinstance(line, str):
            line = {'item': line}
        if not isinstance(line, dict):
            issues.append(f'Item {n}: invalid format.')
            continue
        text = _clean(line.get('item') or line.get('item_name') or line.get('name'), 300)
        cat = None
        score = None
        raw_id = line.get('item_id')
        if raw_id not in (None, ''):
            try:
                cat = by_id.get(int(raw_id))
            except (TypeError, ValueError):
                cat = None
            if cat is None:
                issues.append(f'Item {n}: catalog item_id {raw_id} not found.')
                continue
        elif text and idx is not None and not line.get('no_catalog'):
            # no_catalog=True keeps the caller's exact text (e.g. a line read
            # from a photographed quotation) instead of swapping in the
            # closest catalog item's name.
            exact = [r for r in rows if r['item_name'].strip().lower() == text.lower()]
            if exact:
                cat, score = exact[0], 1.0
            else:
                cat, score = idx.best(text)
        if not text and cat is None:
            issues.append(f'Item {n}: item name missing.')
            continue

        qty = _num(line.get('quantity', line.get('qty')))
        if qty is None or qty <= 0:
            issues.append(f'Item {n} ("{text or cat["item_name"]}"): quantity missing / invalid.')
            continue
        rate = _num(line.get('rate'))
        if rate is None and cat is not None:
            rate = cat['rate_per_unit'] or 0
        if rate is None or rate <= 0:
            sugg = [s['item_name'] for s in search_items(text, 3, rows)] if text else []
            msg = f'Item {n} ("{text}"): catalog mein nahi mila aur rate nahi diya.'
            if sugg:
                msg += ' Milte-julte items: ' + ', '.join(sugg) + '.'
            issues.append(msg)
            continue

        unit = _clean(line.get('unit'), 20) or (cat['unit'] if cat is not None and cat['unit'] else '') \
            or DEFAULT_UNIT
        out.append({
            'item_id': cat['id'] if cat is not None else None,
            'item_name': cat['item_name'] if cat is not None else text,
            'description': _clean(line.get('description'), 2000) or (
                (cat['description'] or '') if cat is not None else ''),
            'specification': _clean(line.get('specification'), 2000) or (
                (cat['specification'] or '') if cat is not None else ''),
            'quantity': qty,
            'unit': unit,
            'rate': rate,
            'requested': text,
            'match_score': score,
            'from_catalog': cat is not None,
        })
    return out, issues


def totals(items):
    sub = sum((it.get('quantity') or 0) * (it.get('rate') or 0) for it in items)
    gst = sum((it.get('quantity') or 0) * (it.get('rate') or 0) * (it.get('gst_percent') or GST_PERCENT)
              / 100.0 for it in items)
    return {'subtotal': round(sub, 2), 'gst': round(gst, 2), 'grand_total': round(sub + gst, 2)}


def _items_form(items):
    f = FormData()
    f['item_id[]'] = [str(it['item_id'] or '') for it in items]
    f['item_name[]'] = [it['item_name'] for it in items]
    f['description[]'] = [it['description'] for it in items]
    f['specification[]'] = [it['specification'] for it in items]
    f['quantity[]'] = [_fmt_num(it['quantity']) for it in items]
    f['unit[]'] = [it['unit'] for it in items]
    f['rate[]'] = [_fmt_num(it['rate']) for it in items]
    return f


# ─────────────────────────────────────────────────────────────
#  Documents (quotations / proforma invoices)
# ─────────────────────────────────────────────────────────────
def create_document(kind, data, user_id):
    """Create a quotation or proforma invoice.

    data: customer (name / id / GSTIN) or customer_id, items [...],
    optional subject, terms, notes, status, date; quotations also take
    enquiry_no / enquiry_date / customer_notes, proformas order_no /
    order_date. preview=True resolves everything and returns the would-be
    document without saving it."""
    kind = kind_of(kind)
    meta = _META[kind]
    if not isinstance(data, dict):
        raise AssistantError('Request body must be a JSON object.')

    cref = data.get('customer_id') or data.get('customer')
    if not cref and isinstance(data.get('new_customer'), dict):
        customer = create_customer(data['new_customer']) if not data.get('preview') else \
            {'id': None, 'company_name': _clean(data['new_customer'].get('company_name'))}
    else:
        customer = resolve_customer(cref)

    items, issues = resolve_items(data.get('items'))
    if issues:
        raise AssistantError(' '.join(issues), 'invalid_items', 422, {'issues': issues})

    status = _clean(data.get('status')) or 'Draft'
    allowed = STATUSES[kind]
    match = [s for s in allowed if s.lower() == status.lower()]
    if not match:
        raise AssistantError(f'Invalid status "{status}". Allowed: {", ".join(allowed)}.')
    status = match[0]
    date = _clean(data.get('date'), 30) or _today()
    subject = _clean(data.get('subject'), 500)
    terms = data.get('terms')
    terms = DEFAULT_TERMS if terms is None else _clean(terms, 5000)
    notes = _clean(data.get('notes'), 5000)

    preview = {
        'type': kind, 'number': None, 'status': status, 'date': date, 'subject': subject,
        'customer': customer, 'items': items, 'totals': totals(items),
    }
    if data.get('preview'):
        preview['preview'] = True
        return preview

    db = get_db()
    try:
        try:
            if kind == QUOTATION:
                enq_no = _clean(data.get('enquiry_no'), 60)
                enq_date = _clean(data.get('enquiry_date'), 30) if enq_no else ''
                cust_notes = _clean(data.get('customer_notes'), 5000)
                doc_id, number = _ops._insert_quotation_row(
                    db, lambda: gen_quotation_no(db),
                    lambda no: (no, date, customer['id'], subject, terms, status, notes, user_id,
                                enq_no, enq_date, cust_notes))
                _ops._save_quotation_items(db, doc_id, _items_form(items))
            else:
                order_no = _clean(data.get('order_no'), 100)
                order_date = _clean(data.get('order_date'), 30)
                doc_id, number = _ops._insert_proforma_row(
                    db, lambda: gen_proforma_no(db),
                    lambda no: (no, order_no, order_date, date, customer['id'], subject, terms,
                                status, notes, user_id))
                _ops._save_proforma_items(db, doc_id, _items_form(items))
        except (QuotationNumberConflictError, ProformaNumberConflictError):
            db.rollback()
            raise AssistantError('Another document was being created at the same moment — '
                                 'nothing was saved, please retry.', 'number_conflict', 409)
        except (QuotationSaveError, ProformaSaveError) as e:
            db.rollback()
            raise AssistantError(' '.join(e.issues), 'invalid_items', 422, {'issues': e.issues})
        db.commit()
    finally:
        db.close()
    return get_document(kind, doc_id)


def _find_doc_row(db, kind, ref):
    meta = _META[kind]
    ref = str(ref or '').strip()
    if ref.isdigit():
        return db.execute(f"SELECT * FROM {meta['table']} WHERE id=?", (int(ref),)).fetchone()
    m = DOC_NO_RE.search(ref)
    if m:
        no = f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}"
        row = db.execute(f"SELECT * FROM {meta['table']} WHERE UPPER({meta['no']})=?",
                         (no,)).fetchone()
        if row is None and m.group(3).isdigit():
            # "AQ-2026-5" -> "AQ-2026-005"
            padded = f"{m.group(1).upper()}-{m.group(2)}-{int(m.group(3)):03d}"
            row = db.execute(f"SELECT * FROM {meta['table']} WHERE UPPER({meta['no']})=?",
                             (padded,)).fetchone()
        return row
    return None


def kind_from_number(ref):
    m = DOC_NO_RE.search(str(ref or ''))
    if not m:
        return None
    return PROFORMA if m.group(1).upper() == 'API' else QUOTATION


def get_document(kind, ref):
    kind = kind_of(kind)
    meta = _META[kind]
    db = get_db()
    try:
        row = _find_doc_row(db, kind, ref)
        if row is None:
            raise AssistantError(f'{meta["label"]} "{ref}" not found.', 'not_found', 404)
        d = dict(row)
        cust = None
        if d.get('customer_id'):
            c = db.execute("SELECT * FROM customers WHERE id=?", (d['customer_id'],)).fetchone()
            cust = _customer_dict(c) if c else None
        items = [dict(r) for r in db.execute(
            f"SELECT * FROM {meta['items']} WHERE {meta['fk']}=? ORDER BY sort_order",
            (d['id'],)).fetchall()]
    finally:
        db.close()
    out = {
        'type': kind, 'id': d['id'], 'number': d[meta['no']], 'status': d.get('status'),
        'date': d.get('date'), 'subject': d.get('subject') or '', 'terms': d.get('terms') or '',
        'notes': d.get('notes') or '', 'created_at': d.get('created_at'),
        'customer': cust,
        'items': [{k: it.get(k) for k in ('item_id', 'item_name', 'description', 'specification',
                                          'quantity', 'unit', 'rate', 'gst_percent')}
                  for it in items],
        'pdf_path': f"/api/v1/{'quotations' if kind == QUOTATION else 'proformas'}/{d['id']}/pdf",
    }
    if kind == QUOTATION:
        out.update(enquiry_no=d.get('enquiry_no') or '', enquiry_date=d.get('enquiry_date') or '')
    else:
        out.update(order_no=d.get('order_no') or '', order_date=d.get('order_date') or '')
    out['totals'] = totals(out['items'])
    return out


def list_documents(kind, q='', status='', limit=10, offset=0):
    kind = kind_of(kind)
    meta = _META[kind]
    try:
        limit = max(1, min(int(limit or 10), 100))
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        limit, offset = 10, 0
    sql = (f"SELECT d.id, d.{meta['no']} AS number, d.date, d.status, d.subject, d.created_at, "
           f"c.company_name, "
           f"(SELECT COALESCE(SUM(quantity*rate),0) FROM {meta['items']} WHERE {meta['fk']}=d.id) AS subtotal "
           f"FROM {meta['table']} d LEFT JOIN customers c ON c.id=d.customer_id WHERE 1=1")
    params = []
    q = _clean(q, 200)
    if q:
        like = f'%{q}%'
        sql += f" AND (d.{meta['no']} LIKE ? OR d.subject LIKE ? OR c.company_name LIKE ?)"
        params += [like, like, like]
    if status:
        sql += " AND LOWER(d.status)=?"
        params.append(str(status).strip().lower())
    sql += " ORDER BY d.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    db = get_db()
    try:
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()
    return [{'type': kind, 'id': r['id'], 'number': r['number'], 'date': r['date'],
             'status': r['status'], 'subject': r['subject'] or '', 'customer': r['company_name'] or '',
             'subtotal': round(r['subtotal'] or 0, 2),
             'grand_total': round((r['subtotal'] or 0) * (1 + GST_PERCENT / 100.0), 2)}
            for r in rows]


def set_status(kind, ref, status):
    kind = kind_of(kind)
    meta = _META[kind]
    allowed = STATUSES[kind]
    match = [s for s in allowed if s.lower() == str(status or '').strip().lower()]
    if not match:
        raise AssistantError(f'Invalid status "{status}". Allowed: {", ".join(allowed)}.')
    db = get_db()
    try:
        row = _find_doc_row(db, kind, ref)
        if row is None:
            raise AssistantError(f'{meta["label"]} "{ref}" not found.', 'not_found', 404)
        db.execute(f"UPDATE {meta['table']} SET status=? WHERE id=?", (match[0], row['id']))
        db.commit()
        doc_id = row['id']
    finally:
        db.close()
    return get_document(kind, doc_id)


def quotation_to_proforma(ref, user_id, order_no='', order_date='', mark_confirmed=False):
    """New proforma invoice with the quotation's customer, subject, terms
    and line items (as stored on the quotation, rates included)."""
    q = get_document(QUOTATION, ref)
    if not q['items']:
        raise AssistantError(f'Quotation {q["number"]} has no items.', 'empty_document')
    data = {
        'customer_id': q['customer']['id'] if q['customer'] else None,
        'subject': q['subject'], 'terms': q['terms'],
        'notes': f'Created from quotation {q["number"]}',
        'order_no': order_no, 'order_date': order_date,
        'items': [{'item_id': None, 'item_name': it['item_name'], 'description': it['description'],
                   'specification': it['specification'], 'quantity': it['quantity'],
                   'unit': it['unit'], 'rate': it['rate']} for it in q['items']],
    }
    if not data['customer_id']:
        raise AssistantError(f'Quotation {q["number"]} has no customer.', 'customer_required')
    # Keep the catalog link where the item still exists.
    for src, dst in zip(q['items'], data['items']):
        dst['item_id'] = src.get('item_id')
    rows = _catalog()
    ids = {r['id'] for r in rows}
    for dst in data['items']:
        if dst['item_id'] not in ids:
            dst['item_id'] = None
    pi = create_document(PROFORMA, data, user_id)
    if mark_confirmed:
        set_status(QUOTATION, q['id'], 'Confirmed')
    pi['source_quotation'] = q['number']
    return pi


def render_pdf(kind, ref):
    """-> (pdf_bytes, filename)"""
    kind = kind_of(kind)
    meta = _META[kind]
    db = get_db()
    try:
        row = _find_doc_row(db, kind, ref)
        if row is None:
            raise AssistantError(f'{meta["label"]} "{ref}" not found.', 'not_found', 404)
        d = dict(row)
        c = None
        if d.get('customer_id'):
            crow = db.execute("SELECT * FROM customers WHERE id=?", (d['customer_id'],)).fetchone()
            # The PDF builders call .upper() etc. on these, so NULL columns
            # (common for customers added by API / import) must become ''.
            c = {k: ('' if v is None else v) for k, v in dict(crow).items()} if crow else None
        items = [dict(r) for r in db.execute(
            f"SELECT * FROM {meta['items']} WHERE {meta['fk']}=? ORDER BY sort_order",
            (d['id'],)).fetchall()]
    finally:
        db.close()
    pdf = build_pdf(d, c, items) if kind == QUOTATION else build_proforma_pdf(d, c, items)
    return pdf, f"{d[meta['no']]}.pdf"


def summary():
    """Counts and values per status, plus this month's totals."""
    month = datetime.now().strftime('%m - %Y')      # dates are stored 'DD - MM - YYYY'
    out = {}
    db = get_db()
    try:
        for kind, meta in _META.items():
            by_status = {}
            for r in db.execute(
                    f"SELECT d.status, COUNT(*) n, "
                    f"COALESCE(SUM((SELECT SUM(quantity*rate) FROM {meta['items']} WHERE {meta['fk']}=d.id)),0) v "
                    f"FROM {meta['table']} d GROUP BY d.status").fetchall():
                by_status[r['status'] or 'Draft'] = {'count': r['n'], 'value': round(r['v'] or 0, 2)}
            m = db.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM((SELECT SUM(quantity*rate) FROM {meta['items']} "
                f"WHERE {meta['fk']}=d.id)),0) v FROM {meta['table']} d WHERE d.date LIKE ?",
                (f'%{month}',)).fetchone()
            out[kind] = {'by_status': by_status,
                         'this_month': {'count': m['n'], 'value': round(m['v'] or 0, 2)}}
        out['customers'] = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        out['items'] = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        db.close()
    return out


# ─────────────────────────────────────────────────────────────
#  Users
# ─────────────────────────────────────────────────────────────
def user_by_username(username):
    db = get_db()
    try:
        row = db.execute("SELECT id, username, full_name, role, COALESCE(is_active,1) active "
                         "FROM users WHERE username=?", (str(username or '').strip(),)).fetchone()
    finally:
        db.close()
    if not row or not row['active']:
        return None
    return dict(row)
