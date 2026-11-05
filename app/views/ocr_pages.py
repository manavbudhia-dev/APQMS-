"""
Scan & Import — upload a scanned/PDF/Word enquiry or purchase order, review
what was read from it, then save that data and/or turn it into a quotation.

Flow
----
  /ocr                  upload form + list of previous imports
  POST /ocr/upload      stores the file, starts background extraction
  /ocr/<id>             "Processing…" (auto-refreshes) -> the review form
  POST /ocr/<id>/save       save the reviewed data (no quotation)
  POST /ocr/<id>/quotation  save it AND create a Draft quotation
  POST /ocr/<id>/delete     remove the import and its uploaded file
  /ocr/<id>/file        download the original upload

OCR is never trusted blindly: nothing becomes a quotation until a person has
seen the extracted rows on the review page. The quotation itself is created
through QuotationPagesMixin._save_quotation_items — the exact code the manual
"New Quotation" form uses — so an OCR-made quotation behaves identically.

Depends on the optional packages in requirements-ocr.txt; the rest of the
app works without them (this page explains what's missing).
"""
import json
import re
from datetime import datetime, timezone

from database import gen_quotation_no, get_db, begin_immediate
from app.ocr import engine, service
from app.views.quotation_pages import QuotationNumberConflictError, QuotationSaveError
from utils.http_utils import (FormData, UploadedFile, consume_csrf_token, esc, file_response,
                              html_response, new_csrf_token, redirect)

# Mirrors the default terms pre-filled by the manual New Quotation page.
DEFAULT_TERMS = ('1. Rates are Ex-works.\n2. GST 18% Extra as applicable.\n'
                 '3. Payment: 20% Advance and Balance against delivery.\n'
                 '4. Delivery: 10-12 weeks after receipt of order.')

UNITS = ['Mtr.', 'Pcs.', 'Kg.', 'Set', 'Box', 'Nos.', 'Ltr.', 'Pair', 'Roll', 'Bag', 'Ton', 'Ft.']
MAX_ACTIVE_PER_USER = 5          # documents one user may have processing at once
MAX_CATALOG_IN_PAGE = 3000
FIELD_LIMITS = {'company_name': 200, 'contact_person': 120, 'address': 400, 'phone': 60, 'email': 120,
                'gstin': 20, 'doc_reference': 60, 'doc_date': 30, 'subject': 200}

STATUS_BADGE = {service.STATUS_PROCESSING: 'badge-draft', service.STATUS_READY: 'badge-draft',
                service.STATUS_SAVED: 'badge-sent', service.STATUS_QUOTED: 'badge-confirmed',
                service.STATUS_FAILED: 'badge-cancelled'}
METHOD_LABEL = {'text_layer': 'Read from the PDF\'s own text', 'ocr': 'Scanned — read with OCR',
                'mixed': 'Mixed: text pages + scanned pages', 'docx': 'Read from the Word document',
                'docx+ocr': 'Word document + OCR of its pictures', 'image': 'Image — read with OCR'}


def _fmt_num(v):
    if v is None:
        return ''
    return ('%.2f' % v).rstrip('0').rstrip('.') if v != int(v) else str(int(v))


def _local_time(ts):
    try:
        dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime('%d %b %Y, %H:%M')
    except (TypeError, ValueError):
        return ts or ''


def _catalog_label(row):
    label = f"#{row['id']} · {row['item_name']}"
    desc = (row['description'] or '').strip()
    if desc and desc.lower() != (row['item_name'] or '').strip().lower():
        label += f' — {desc}'
    return label[:150]


class OcrPagesMixin:

    # ── helpers ──────────────────────────────────────────────
    def _ocr_alert(self, msg, err=False, ok=False):
        if not msg:
            return ''
        if err:
            return f'<div class="alert alert-danger py-2 mb-3"><i class="bi bi-exclamation-triangle"></i> {esc(msg)}</div>'
        cls = 'alert-success' if ok else 'alert-info'
        return f'<div class="alert {cls} py-2 mb-3"><i class="bi bi-check-circle"></i> {esc(msg)}</div>'

    def _ocr_page(self, title, heading, content):
        return self._layout(title, heading, content, 'ocr')

    # ── LIST + UPLOAD PAGE ───────────────────────────────────
    def pg_ocr(self, user_id, msg='', err=False):
        caps = engine.capabilities()
        db = get_db()
        try:
            docs = service.list_documents(db)
        finally:
            db.close()
        is_admin = self._is_admin(user_id)

        notices = ''
        if not caps['tesseract']:
            notices = f'''<div class="alert alert-warning mb-3">
  <b><i class="bi bi-exclamation-triangle"></i> Scanned files and images can't be read yet.</b>
  {esc(caps['detail'])}<br>
  Digital PDFs and Word (.docx) files that contain real text still work without it.
  <details class="mt-2"><summary>How to enable OCR</summary>
    <ol class="mb-0 mt-2">
      <li><code>pip install -r requirements-ocr.txt</code></li>
      <li>Install the Tesseract program: Windows &mdash; the installer from
          <i>UB-Mannheim/tesseract</i>; Ubuntu/Debian &mdash; <code>sudo apt install tesseract-ocr</code>;
          macOS &mdash; <code>brew install tesseract</code>.</li>
      <li>If it isn't found automatically, set <code>TESSERACT_CMD</code> to the full path of the
          tesseract program, then restart the server.</li>
    </ol></details></div>'''
        else:
            missing = [n for n, ok in (('PDF', caps['pdf']), ('Word', caps['docx'])) if not ok]
            if missing:
                notices += (f'<div class="alert alert-warning py-2 mb-3">{" and ".join(missing)} support is not '
                            'installed. Run <code>pip install -r requirements-ocr.txt</code> and restart.</div>')
            if caps['missing_langs']:
                notices += (f'<div class="alert alert-warning py-2 mb-3">OCR language(s) '
                            f'{esc(", ".join(caps["missing_langs"]))} are not installed for Tesseract; '
                            f'using <b>{esc(caps["lang"])}</b>.</div>')

        token = new_csrf_token()
        max_mb = engine.MAX_UPLOAD_BYTES // (1024 * 1024)
        status_line = (f'Tesseract {esc(caps["version"])} · language: {esc(caps["lang"])}'
                       if caps['tesseract'] else 'Tesseract not available')
        upload = f'''<div class="form-section mb-3">
  <h6 class="mb-3"><i class="bi bi-file-earmark-arrow-up"></i> Upload a document</h6>
  <form method="POST" action="/ocr/upload" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{esc(token)}">
    <div class="row g-3 align-items-end">
      <div class="col-md-7">
        <label class="form-label">Enquiry / purchase order / item list</label>
        <input type="file" name="file" class="form-control" required
               accept=".pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp">
        <div class="form-text">PDF, Word (.docx) or a scan/photo (PNG, JPG, TIFF). Up to {max_mb} MB.</div>
      </div>
      <div class="col-md-5">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" name="force_ocr" value="1" id="forceOcr">
          <label class="form-check-label" for="forceOcr">Ignore the file's embedded text and OCR it</label>
        </div>
        <div class="form-text">Only for PDFs whose text is wrong or garbled.</div>
      </div>
    </div>
    <div class="d-flex align-items-center gap-3 mt-3">
      <button type="submit" class="btn btn-primary"><i class="bi bi-upload"></i> Read document</button>
      <span class="text-muted small">{status_line}</span>
    </div>
  </form>
</div>'''

        if docs:
            rows = ''
            for d in docs:
                who = d['customer_name'] or d['company_name'] or '—'
                conf = ''
                if d['ocr_confidence'] is not None:
                    c = d['ocr_confidence']
                    col = '#16A34A' if c >= 80 else ('#D97706' if c >= engine.LOW_CONFIDENCE else '#DC2626')
                    conf = f'<span style="color:{col};font-weight:600">{c:.0f}%</span>'
                else:
                    conf = '<span class="text-muted">exact text</span>' if d['status'] != service.STATUS_FAILED else ''
                quote = (f'<a href="/quotations/{d["quotation_id"]}" class="ms-1 small">'
                         f'{esc(d["quotation_no"])}</a>' if d['quotation_id'] and d['quotation_no'] else '')
                can_delete = is_admin or d['created_by'] == user_id
                delete = (f'''<form method="POST" action="/ocr/{d["id"]}/delete" class="d-inline"
                     onsubmit="return confirm('Delete this import and its uploaded file?')">
                     <button class="btn btn-sm btn-outline-danger" title="Delete"><i class="bi bi-trash"></i></button>
                     </form>''' if can_delete else '')
                rows += f'''<tr>
  <td><a href="/ocr/{d["id"]}"><i class="bi bi-file-earmark-text"></i> {esc(d["original_filename"])}</a></td>
  <td>{esc(who)}</td>
  <td>{d["item_count"]}</td>
  <td><span class="badge {STATUS_BADGE.get(d["status"], "badge-draft")}">{esc(d["status"])}</span>{quote}</td>
  <td>{conf}</td>
  <td class="text-muted small">{esc(_local_time(d["created_at"]))}<br>{esc(d["uploader"] or "")}</td>
  <td class="text-end text-nowrap"><a class="btn btn-sm btn-outline-primary" href="/ocr/{d["id"]}">Open</a> {delete}</td>
</tr>'''
            history = f'''<div class="table-card">
  <div class="card-header"><h6 class="mb-0">Recent imports</h6></div>
  <div class="table-responsive-wrapper"><table class="table mb-0">
    <thead><tr><th>Document</th><th>Customer</th><th>Items</th><th>Status</th><th>Scan quality</th>
    <th>Uploaded</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></div>'''
        else:
            history = '''<div class="table-card"><div class="card-body text-center py-5">
  <i class="bi bi-file-earmark-arrow-up" style="font-size:2.5rem;color:#94A3B8"></i>
  <h5 class="mt-3">Nothing imported yet</h5>
  <p class="text-muted mb-0">Upload an enquiry or purchase order above to turn it into a quotation.</p>
</div></div>'''

        content = self._ocr_alert(msg, err=err) + notices + upload + history
        return self._ocr_page('Scan & Import', 'Scan & Import', content)

    # ── UPLOAD ───────────────────────────────────────────────
    def handle_ocr_upload(self, form, user_id):
        def fail(message):
            return self.send(*html_response(self.pg_ocr(user_id, msg=message, err=True), status=400))

        if not consume_csrf_token(form.get('csrf_token', '')):
            return fail('Your session expired — please choose the file and try again.')
        up = form.get('file')
        if not isinstance(up, UploadedFile) or not up.data:
            return fail('Please choose a file to upload.')
        if len(up.data) > engine.MAX_UPLOAD_BYTES:
            return fail(f'That file is larger than the {engine.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.')

        kind, ext_or_reason = engine.sniff_file_type(up.data)
        if kind is None:
            return fail(ext_or_reason)
        caps = engine.capabilities()
        if kind == 'image' and not caps['tesseract']:
            return fail('Images need the Tesseract OCR program, which is not available on this server.')
        if kind == 'pdf' and not caps['pdf']:
            return fail('PDF support is not installed (pip install -r requirements-ocr.txt).')
        if kind == 'docx' and not caps['docx']:
            return fail('Word support is not installed (pip install -r requirements-ocr.txt).')

        db = get_db()
        try:
            active = db.execute("SELECT COUNT(*) FROM ocr_documents WHERE created_by=? AND status=? "
                                "AND created_at > datetime('now','-15 minutes')",
                                (user_id, service.STATUS_PROCESSING)).fetchone()[0]
        finally:
            db.close()
        if active >= MAX_ACTIVE_PER_USER:
            return fail('You already have several documents being read. Please wait for them to finish.')

        doc_id = service.create_document(up.filename or 'document', kind, ext_or_reason, up.data, user_id)
        service.start_processing(doc_id, force_ocr=form.get('force_ocr') == '1')
        return self.send(*redirect(f'/ocr/{doc_id}'))

    # ── DETAIL / REVIEW PAGE ─────────────────────────────────
    def pg_ocr_detail(self, user_id, doc_id, msg='', err=False, ok=False):
        db = get_db()
        try:
            doc = service.get_document(db, doc_id)
            if doc is None:
                return self.pg_404()
            items = service.get_items(db, doc_id)
            customers = db.execute("SELECT id, company_name, gstin FROM customers "
                                   "ORDER BY company_name").fetchall()
            catalog = db.execute("SELECT id, item_name, description, specification, unit, rate_per_unit "
                                 "FROM items ORDER BY item_name LIMIT ?", (MAX_CATALOG_IN_PAGE,)).fetchall()
        finally:
            db.close()

        badge = (f'<span class="badge {STATUS_BADGE.get(doc["status"], "badge-draft")}">'
                 f'{esc(doc["status"])}</span>')
        back = '<a href="/ocr" class="btn btn-sm btn-outline-secondary"><i class="bi bi-arrow-left"></i> All imports</a>'
        alert = self._ocr_alert(msg, err=err, ok=ok)

        if doc['status'] == service.STATUS_PROCESSING:
            content = f'''<div class="mb-3">{back}</div>{alert}
<div class="table-card"><div class="card-body text-center py-5">
  <div class="spinner-border text-primary" role="status"></div>
  <h5 class="mt-3">Reading {esc(doc["original_filename"])}…</h5>
  <p class="text-muted mb-0">Scanned pages can take up to a minute. This page updates by itself.</p>
</div></div>
<script>setTimeout(function(){{ location.reload(); }}, 2500);</script>'''
            return self._ocr_page('Scan & Import', 'Reading document…', content)

        if doc['status'] == service.STATUS_FAILED:
            content = f'''<div class="mb-3">{back}</div>{alert}
<div class="table-card"><div class="card-body py-4">
  <h5 style="color:#DC2626"><i class="bi bi-exclamation-triangle"></i> This document could not be read</h5>
  <p class="mt-3">{esc(doc["error_message"] or "Unknown problem.")}</p>
  <form method="POST" action="/ocr/{doc_id}/delete" class="d-inline">
    <button class="btn btn-outline-danger">Remove this import</button></form>
  <a href="/ocr" class="btn btn-primary ms-2">Try another file</a>
</div></div>'''
            return self._ocr_page('Scan & Import', esc(doc['original_filename']), content)

        return self._ocr_review(user_id, doc, items, customers, catalog, badge, back, alert)

    def _ocr_review(self, user_id, doc, items, customers, catalog, badge, back, alert):
        doc_id = doc['id']
        ready = doc['status'] == service.STATUS_READY
        try:
            warnings = json.loads(doc['warnings'] or '[]')
        except ValueError:
            warnings = []
        warn_html = ''.join(f'<div class="alert alert-warning py-2 mb-2"><i class="bi bi-info-circle"></i> '
                            f'{esc(w)}</div>' for w in warnings)

        by_id = {c['id']: c for c in catalog}
        catalog_json = {str(c['id']): {'n': c['item_name'], 'd': c['description'] or '',
                                       's': c['specification'] or '', 'u': c['unit'] or '',
                                       'r': c['rate_per_unit'] or 0} for c in catalog}
        datalist = ''.join(f'<option value="{esc(_catalog_label(c))}"></option>' for c in catalog)
        unit_list = ''.join(f'<option value="{esc(u)}"></option>' for u in UNITS)

        # customer selector
        sel = doc['customer_id']
        choice = str(sel) if sel else ('new' if (doc['company_name'] or '').strip() else '')
        opts = (f'<option value="new"{" selected" if choice == "new" else ""}>'
                '➕ Create a new customer from the details below</option>'
                f'<option value=""{" selected" if choice == "" else ""}>— Not chosen —</option>')
        for c in customers:
            s = ' selected' if str(c['id']) == choice else ''
            opts += f'<option value="{c["id"]}"{s}>{esc(c["company_name"])}</option>'
        match_note = ''
        if doc['customer_match'] and sel:
            how = 'GSTIN' if doc['customer_match'] == 'GSTIN' else 'company name'
            match_note = (f'<div class="form-text text-success"><i class="bi bi-check-circle"></i> '
                          f'Matched to an existing customer by {how}.</div>')

        def field(name, label, val, cols='col-md-6', tag='input', **extra):
            v = esc(val or '')
            if tag == 'textarea':
                return (f'<div class="{cols}"><label class="form-label">{label}</label>'
                        f'<textarea name="{name}" rows="3" class="form-control" '
                        f'maxlength="{FIELD_LIMITS[name]}">{v}</textarea></div>')
            return (f'<div class="{cols}"><label class="form-label">{label}</label>'
                    f'<input name="{name}" class="form-control" value="{v}" '
                    f'maxlength="{FIELD_LIMITS[name]}"></div>')

        def item_row(it=None):
            it = it or {}
            g = lambda k: it[k] if it and k in it.keys() and it[k] is not None else ''
            label = _catalog_label(by_id[it['item_id']]) if it and it['item_id'] in by_id else ''
            score = it['match_score'] if it and it['match_score'] is not None else None
            if label:
                hint = (f'<div class="small text-success">Matched to catalog'
                        f'{" (" + str(int(round(score * 100))) + "%)" if score and ready else ""}</div>')
            elif it and ready:
                hint = '<div class="small text-muted">Not in catalog</div>'
            else:
                hint = ''
            cat_rate = ''
            if it and it['catalog_rate']:
                cat_rate = f'<div class="small text-muted">Catalog: ₹{_fmt_num(it["catalog_rate"])}</div>'
            flags = ''
            if ready and it and it['flags']:
                flags = ''.join(f'<div class="small" style="color:#B45309"><i class="bi bi-flag-fill"></i> '
                                f'{esc(f)}</div>' for f in it['flags'].split(service.FLAG_SEP) if f)
            raw = esc(it['raw_text']) if it and it['raw_text'] else ''
            rid = it['id'] if it else ''
            return f'''<tr>
  <td class="text-muted" title="Read as: {raw}"><span class="ocr-n"></span>
      <input type="hidden" name="row_id[]" value="{rid}"></td>
  <td style="min-width:240px">
    <input name="item_name[]" class="form-control form-control-sm mb-1" placeholder="Item name" value="{esc(g("item_name"))}" maxlength="200">
    <textarea name="description[]" rows="2" class="form-control form-control-sm" placeholder="Description" maxlength="1000">{esc(g("description"))}</textarea>
    <input type="hidden" name="specification[]" value="{esc(g("specification"))}">
  </td>
  <td style="min-width:220px">
    <input name="catalog_name[]" list="ocrCatalog" class="form-control form-control-sm ocr-catalog" placeholder="Search catalog…" value="{esc(label)}" autocomplete="off">{hint}
  </td>
  <td style="width:96px"><input name="quantity[]" class="form-control form-control-sm" inputmode="decimal" value="{_fmt_num(it["quantity"]) if it else ""}"></td>
  <td style="width:96px"><input name="unit[]" list="ocrUnits" class="form-control form-control-sm" value="{esc(g("unit"))}" maxlength="20"></td>
  <td style="width:120px"><input name="rate[]" class="form-control form-control-sm" inputmode="decimal" value="{_fmt_num(it["rate"]) if it else ""}">{cat_rate}</td>
  <td style="min-width:150px">{flags}</td>
  <td><button type="button" class="btn btn-sm btn-outline-danger ocr-del" title="Remove row"><i class="bi bi-x-lg"></i></button></td>
</tr>'''

        rows_html = ''.join(item_row(it) for it in items)
        empty_note = '' if items else ('<div class="alert alert-warning py-2">No line items were recognised. '
                                       'Add them with the button below.</div>')

        info = [f'<b>{esc(doc["original_filename"])}</b>', esc(doc['file_type'].upper()),
                f'{doc["page_count"]} page{"s" if doc["page_count"] != 1 else ""}',
                esc(METHOD_LABEL.get(doc['extraction_method'], doc['extraction_method'] or ''))]
        if doc['ocr_confidence'] is not None:
            info.append(f'OCR confidence {doc["ocr_confidence"]:.0f}%')
        quoted = ''
        if doc['quotation_id'] and doc['quotation_no']:
            quoted = (f'<div class="alert alert-success py-2 mb-3"><i class="bi bi-check-circle"></i> '
                      f'A quotation was already created from this document: '
                      f'<a href="/quotations/{doc["quotation_id"]}">{esc(doc["quotation_no"])}</a>.</div>')

        raw_text = esc((doc['raw_text'] or '')[:60000])
        create_label = 'Create another quotation' if doc['quotation_id'] else 'Create quotation'
        confirm = (' onclick="return confirm(\'A quotation was already created from this document. '
                   'Create another one?\')"') if doc['quotation_id'] else ''

        script = '''<script>
(function(){
  var CAT = __CATALOG__;
  var body = document.getElementById('ocrRows');
  var tpl = document.getElementById('ocrRowTpl');
  function renumber(){ Array.prototype.forEach.call(body.rows, function(r,i){
    r.querySelector('.ocr-n').textContent = i + 1; }); }
  renumber();
  document.getElementById('ocrAddRow').addEventListener('click', function(){
    body.appendChild(tpl.content.cloneNode(true)); renumber(); });
  body.addEventListener('click', function(e){
    var b = e.target.closest('.ocr-del'); if (b) { b.closest('tr').remove(); renumber(); } });
  body.addEventListener('change', function(e){
    if (!e.target.classList.contains('ocr-catalog')) return;
    var m = /^#(\\d+)\\b/.exec(e.target.value), c = m && CAT[m[1]];
    if (!c) return;
    var tr = e.target.closest('tr');
    function set(n, v){ tr.querySelector('[name="' + n + '"]').value = v; }
    set('item_name[]', c.n); set('description[]', c.d); set('specification[]', c.s);
    if (c.u) set('unit[]', c.u);
    var rate = tr.querySelector('[name="rate[]"]');
    if (c.r && (!rate.value || parseFloat(rate.value) === 0)) rate.value = c.r;
  });
})();
</script>'''.replace('__CATALOG__', json.dumps(catalog_json).replace('<', '\\u003c'))

        content = f'''<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
  <div>{back} <a href="/ocr/{doc_id}/file" class="btn btn-sm btn-outline-secondary ms-1"><i class="bi bi-download"></i> Original file</a></div>
  <div>{badge}</div>
</div>
<div class="text-muted small mb-3">{" · ".join(info)}</div>
{alert}{quoted}{warn_html}
{('<div class="alert alert-info py-2 mb-3"><i class="bi bi-eye"></i> Automatic reading can make mistakes. '
  'Check every name, quantity and rate against the original before saving.</div>') if ready else ''}
<form method="POST" action="/ocr/{doc_id}/save" id="ocrForm">
  <div class="form-section mb-3">
    <h6 class="mb-3"><i class="bi bi-person-badge"></i> Customer</h6>
    <div class="row g-3">
      <div class="col-md-8"><label class="form-label">Customer for the quotation</label>
        <select name="customer_id" class="form-select">{opts}</select>{match_note}</div>
      {field("company_name", "Company name", doc["company_name"])}
      {field("contact_person", "Contact person", doc["contact_person"])}
      {field("phone", "Phone", doc["phone"], "col-md-4")}
      {field("email", "Email", doc["email"], "col-md-4")}
      {field("gstin", "GSTIN", doc["gstin"], "col-md-4")}
      {field("address", "Address", doc["address"], "col-12", "textarea")}
    </div>
    <div class="form-text mt-2">These details are only used when a new customer is created.
      If you pick an existing customer, that customer's stored details are kept.</div>
  </div>
  <div class="form-section mb-3">
    <h6 class="mb-3"><i class="bi bi-card-text"></i> Document</h6>
    <div class="row g-3">
      {field("doc_reference", "Their reference / enquiry / PO no.", doc["doc_reference"], "col-md-4")}
      {field("doc_date", "Date on the document", doc["doc_date"], "col-md-4")}
      {field("subject", "Quotation subject", doc["subject"], "col-md-4")}
    </div>
  </div>
  <div class="form-section mb-3">
    <h6 class="mb-3"><i class="bi bi-list-ol"></i> Items</h6>
    {empty_note}
    <div class="table-responsive-wrapper"><table class="table align-top mb-2">
      <thead><tr><th>#</th><th>Item as read</th><th>Catalog item</th><th>Qty</th><th>Unit</th>
      <th>Rate (₹)</th><th>Notes</th><th></th></tr></thead>
      <tbody id="ocrRows">{rows_html}</tbody></table></div>
    <button type="button" id="ocrAddRow" class="btn btn-sm btn-outline-primary"><i class="bi bi-plus-lg"></i> Add row</button>
    <div class="form-text mt-2">Picking a catalog item fills in its name, description, unit and rate.
      A quotation needs a quantity and a rate on every row.</div>
  </div>
  <div class="d-flex gap-2 flex-wrap mb-3">
    <button type="submit" formaction="/ocr/{doc_id}/save" class="btn btn-outline-primary"><i class="bi bi-save"></i> Save data</button>
    <button type="submit" formaction="/ocr/{doc_id}/quotation" class="btn btn-primary"{confirm}><i class="bi bi-file-earmark-plus"></i> {create_label}</button>
  </div>
  <datalist id="ocrCatalog">{datalist}</datalist>
  <datalist id="ocrUnits">{unit_list}</datalist>
</form>
<template id="ocrRowTpl">{item_row(None)}</template>
<details class="mb-3"><summary class="text-muted">Show the raw text that was read</summary>
  <pre style="tab-size:4;white-space:pre-wrap;max-height:420px;overflow:auto;background:#F8FAFC;
  border:1px solid #E2E8F0;border-radius:8px;padding:12px;font-size:12px">{raw_text}</pre></details>
<form method="POST" action="/ocr/{doc_id}/delete" onsubmit="return confirm('Delete this import and its uploaded file?')">
  <button class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i> Delete this import</button></form>
{script}'''
        return self._ocr_page('Scan & Import', esc(doc['original_filename']), content)

    # ── REVIEW FORM -> DATA ──────────────────────────────────
    def _ocr_read_form(self, form):
        """-> (customer_choice, header, rows, notes). `notes` lists numbers
        that couldn't be read (cleared, not silently kept)."""
        header = {f: (form.get(f) or '').strip()[:lim] for f, lim in FIELD_LIMITS.items()}
        choice = (form.get('customer_id') or '').strip()

        db = get_db()
        try:
            valid_items = {r['id'] for r in db.execute("SELECT id FROM items")}
        finally:
            db.close()

        lists = {k: form.getlist(f'{k}[]') for k in
                 ('row_id', 'item_name', 'description', 'specification', 'catalog_name',
                  'quantity', 'unit', 'rate')}
        n = min(len(v) for v in lists.values())
        rows, notes = [], []
        for i in range(n):
            name = lists['item_name'][i].strip()[:200]
            desc = lists['description'][i].strip()[:1000]
            qty_raw, rate_raw = lists['quantity'][i].strip(), lists['rate'][i].strip()
            if not (name or desc or qty_raw or rate_raw):
                continue                                   # a blank row the user never filled
            qty = rate = None
            if qty_raw:
                qty = self._parse_numeric_field(qty_raw)
                if qty is None:
                    notes.append(f'Row {len(rows) + 1}: quantity "{qty_raw}" is not a number — cleared.')
            if rate_raw:
                rate = self._parse_numeric_field(rate_raw)
                if rate is None:
                    notes.append(f'Row {len(rows) + 1}: rate "{rate_raw}" is not a number — cleared.')
            m = re.match(r'#(\d+)\b', lists['catalog_name'][i].strip())
            item_id = int(m.group(1)) if m and int(m.group(1)) in valid_items else None
            try:
                row_id = int(lists['row_id'][i]) if lists['row_id'][i] else None
            except ValueError:
                row_id = None
            rows.append({'row_id': row_id, 'item_id': item_id, 'item_name': name or desc.split('\n')[0][:200],
                         'description': desc if name else '', 'specification': lists['specification'][i].strip()[:1000],
                         'quantity': qty, 'unit': lists['unit'][i].strip()[:20], 'rate': rate})
        return choice, header, rows, notes

    def _ocr_editable_doc(self, db, doc_id):
        doc = service.get_document(db, doc_id)
        if doc is None or doc['status'] in (service.STATUS_PROCESSING, service.STATUS_FAILED):
            return None
        return doc

    def _ocr_persist(self, db, doc, choice, header, rows):
        customer_id = int(choice) if choice.isdigit() and db.execute(
            "SELECT 1 FROM customers WHERE id=?", (int(choice),)).fetchone() else None
        status = doc['status'] if doc['status'] == service.STATUS_QUOTED else service.STATUS_SAVED
        service.save_review(db, doc['id'], customer_id, header, rows, status)
        return customer_id

    # ── ACTION: save the reviewed data ───────────────────────
    def handle_ocr_save(self, doc_id, form, user_id):
        db = get_db()
        try:
            doc = self._ocr_editable_doc(db, doc_id)
            if doc is None:
                return self.send(*redirect(f'/ocr/{doc_id}'))
            choice, header, rows, notes = self._ocr_read_form(form)
            self._ocr_persist(db, doc, choice, header, rows)
            db.commit()
        finally:
            db.close()
        if notes:
            return self.send(*html_response(self.pg_ocr_detail(
                user_id, doc_id, msg='Saved. ' + ' '.join(notes), ok=True)))
        return self.send(*html_response(self.pg_ocr_detail(user_id, doc_id, msg='Data saved.', ok=True)))

    # ── ACTION: save + create a Draft quotation ──────────────
    def handle_ocr_quotation(self, doc_id, form, user_id):
        def problem(message):
            return self.send(*html_response(self.pg_ocr_detail(user_id, doc_id, msg=message, err=True), 400))

        db = get_db()
        try:
            doc = self._ocr_editable_doc(db, doc_id)
            if doc is None:
                return self.send(*redirect(f'/ocr/{doc_id}'))
            choice, header, rows, notes = self._ocr_read_form(form)
            self._ocr_persist(db, doc, choice, header, rows)   # keep the edits even if we refuse below
            db.commit()

            issues = list(notes)
            if not rows:
                issues.append('Add at least one item.')
            for i, r in enumerate(rows, 1):
                if not r['item_name']:
                    issues.append(f'Row {i}: needs an item name.')
                if not r['quantity'] or r['quantity'] <= 0:
                    issues.append(f'Row {i} ("{r["item_name"]}"): needs a quantity.')
                if not r['rate'] or r['rate'] <= 0:
                    issues.append(f'Row {i} ("{r["item_name"]}"): needs a rate — enter one or pick the '
                                  'catalog item.')
            if choice == 'new' and not header['company_name']:
                issues.append('Enter the company name to create a new customer.')
            elif choice != 'new' and not choice.isdigit():
                issues.append('Choose an existing customer, or "Create a new customer".')
            if issues:
                return problem('Your changes were saved, but the quotation was not created: '
                               + ' '.join(issues))

            customer_id = int(choice) if choice.isdigit() else None
            if customer_id is not None and not db.execute(
                    "SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                return problem('That customer no longer exists — pick another.')

            # Everything from here on creates rows, so take the write lock
            # *before* asking the database whether this scan has been
            # quoted yet. Two reviewers — or one reviewer pressing the
            # button twice — otherwise both read "not quoted", both create a
            # quotation, and the second UPDATE at the end re-links the
            # document, orphaning the first quotation with nobody able to
            # find it from the scan.
            begin_immediate(db)
            if db.execute("SELECT 1 FROM ocr_documents WHERE id=? AND quotation_id IS NOT NULL",
                          (doc_id,)).fetchone():
                db.rollback()
                return problem('A quotation has already been created from this scan — '
                               'edit that quotation instead of making another.')

            if choice == 'new':
                customer_id = db.execute(
                    "INSERT INTO customers (contact_person, company_name, address, phone, email, gstin) "
                    "VALUES (?,?,?,?,?,?)",
                    (header['contact_person'], header['company_name'], header['address'],
                     header['phone'], header['email'], header['gstin'])).lastrowid

            note = f'Created from scanned document "{doc["original_filename"]}"'
            if header['doc_reference']:
                note += f' (their ref: {header["doc_reference"]}'
                note += f', dated {header["doc_date"]})' if header['doc_date'] else ')'
            date = datetime.now().strftime('%d - %m - %Y')

            try:
                qid, _ = self._insert_quotation_row(
                    db, lambda: gen_quotation_no(db),
                    lambda qno: (qno, date, customer_id, header['subject'], DEFAULT_TERMS,
                                 'Draft', note, user_id, '', '', ''))
            except QuotationNumberConflictError:
                db.rollback()
                return problem('Another quotation was being created at the same moment. '
                               'Nothing was created — please press the button again.')

            qform = FormData()
            qform['item_id[]'] = [str(r['item_id'] or '') for r in rows]
            qform['item_name[]'] = [r['item_name'] for r in rows]
            qform['description[]'] = [r['description'] for r in rows]
            qform['specification[]'] = [r['specification'] for r in rows]
            qform['quantity[]'] = [_fmt_num(r['quantity']) for r in rows]
            qform['unit[]'] = [r['unit'] for r in rows]
            qform['rate[]'] = [_fmt_num(r['rate']) for r in rows]
            try:
                self._save_quotation_items(db, qid, qform)
            except QuotationSaveError as e:
                db.rollback()                                   # also undoes the new customer
                return problem('The quotation was not created: ' + ' '.join(e.issues))

            db.execute("UPDATE ocr_documents SET customer_id=?, quotation_id=?, status=?, "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (customer_id, qid, service.STATUS_QUOTED, doc_id))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect(f'/quotations/{qid}'))

    # ── ACTION: delete ───────────────────────────────────────
    def handle_ocr_delete(self, doc_id, user_id):
        db = get_db()
        try:
            doc = db.execute("SELECT created_by FROM ocr_documents WHERE id=?", (doc_id,)).fetchone()
            if doc is not None and (doc['created_by'] == user_id or self._is_admin(user_id)):
                service.delete_document(db, doc_id)
        finally:
            db.close()
        return self.send(*redirect('/ocr'))

    # ── DOWNLOAD the original upload ─────────────────────────
    def serve_ocr_file(self, doc_id):
        db = get_db()
        try:
            doc = db.execute("SELECT original_filename, stored_filename FROM ocr_documents WHERE id=?",
                             (doc_id,)).fetchone()
        finally:
            db.close()
        data = service.read_original(doc['stored_filename']) if doc else None
        if data is None:
            return self.send(*html_response(self.pg_404(), 404))
        ext = '.' + doc['stored_filename'].rsplit('.', 1)[-1]
        return self.send(*file_response(data, service.safe_download_name(doc['original_filename']),
                                        service.CONTENT_TYPES.get(ext, 'application/octet-stream')))
