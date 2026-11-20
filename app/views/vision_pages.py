"""
Photo Quote (/photo-quote): upload a photo of a hand-edited quotation
sheet, let Claude read it, review / correct the values, then create a
normal quotation (PDF from the existing quotation template).

Rate, quantity and delivery must each be ticked as checked by the user
before a quotation is made; anything the model flagged (needs_review) is
highlighted with its reason. The original photo and every stage of the
data are kept for audit (see app/vision/store.py).
"""
from database import get_db
from app.assistant import core
from app.vision import confirm, extractor, store
from app.vision.schema import FIELD_LABELS, MUST_CONFIRM, REQUIRED_FIELDS, empty_extraction
from utils.http_utils import (UploadedFile, consume_csrf_token, esc, file_response, html_response,
                              new_csrf_token, redirect)

MAX_PER_HOUR = 30            # extractions per user per hour (each one is a paid API call)

_FIELDS = [   # (form name, schema path, input kind)
    ('product', 'product', 'text'),
    ('link_material', 'link_material', 'text'),
    ('pin', 'pin', 'text'),
    ('attachment_material', 'attachment.material', 'text'),
    ('attachment_height_mm', 'attachment.height_mm', 'number'),
    ('attachment_thickness_mm', 'attachment.thickness_mm', 'number'),
    ('total_width_mm', 'total_width_mm', 'number'),
    ('quantity_mtrs', 'quantity_mtrs', 'number'),
    ('rate_per_mtr', 'rate_per_mtr', 'number'),
    ('original_rate_per_mtr', 'original_rate_per_mtr', 'number'),
    ('gst_percent', 'gst_percent', 'number'),
    ('price_basis', 'price_basis', 'text'),
    ('delivery_weeks', 'delivery_weeks', 'number'),
    ('payment_terms', 'payment_terms', 'text'),
    ('notes', 'notes', 'textarea'),
]


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


def _fmt(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


class VisionPagesMixin:

    def _vision_extract(self, data):
        """Seam for tests: replaced with a fake in tests/test_vision.py."""
        return extractor.extract(data)

    # ── list + upload ────────────────────────────────────────
    def pg_photo_quote(self, user_id, msg='', err=False):
        ok, reason = extractor.available()
        csrf = new_csrf_token()
        alert = ''
        if msg:
            alert = f'<div class="alert alert-{"danger" if err else "info"}">{esc(msg)}</div>'
        if not ok:
            alert += (f'<div class="alert alert-warning"><strong>Setup needed:</strong> {esc(reason)} '
                      'See requirements-vision.txt.</div>')
        rows = ''
        for r in store.recent():
            if r['quotation_id']:
                state = (f'<a href="/quotations/{r["quotation_id"]}" class="badge bg-success">'
                         f'{esc(r["quotation_no"] or "Quotation")}</a>')
            elif r['status'] == store.STATUS_FAILED:
                state = '<span class="badge bg-danger">Failed</span>'
            else:
                state = '<span class="badge bg-warning text-dark">Needs review</span>'
            rows += (f'<tr><td><a href="/photo-quote/{r["id"]}">#{r["id"]}</a></td>'
                     f'<td>{esc(r["original_filename"] or "")}</td><td>{esc(r["username"] or "")}</td>'
                     f'<td>{esc(r["created_at"] or "")}</td><td>{state}</td></tr>')
        if not rows:
            rows = '<tr><td colspan="5" class="text-muted">Abhi koi photo upload nahi hui.</td></tr>'
        ai_box = ''
        if self._is_admin(user_id):
            st = extractor.ai_settings()
            ai_box = f'''
<div class="form-section mt-4">
  <h5><i class="bi bi-plug"></i> AI connection</h5>
  <div class="small mb-2">Base URL: <code>{esc(st['base_url'] or 'Anthropic direct')}</code> ·
    Model: <code>{esc(st['model'])}</code> · Key: {'set' if st['api_key'] else '<b class="text-danger">not set</b>'} ·
    Compat: <code>{esc(st['compat'])}</code> ·
    Protocol: <code>{esc(st['protocol'] + (' → openai' if st['protocol'] == 'auto' and extractor._use_openai() else ''))}</code></div>
  <form method="post" action="/photo-quote/ai-test" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').innerText='Testing… (20-40 sec)';">
    <input type="hidden" name="csrf_token" value="{esc(csrf)}">
    <button class="btn btn-sm btn-outline-primary" {"disabled" if not ok else ""}><i class="bi bi-lightning"></i> Test AI connection</button>
  </form>
  <small class="text-muted">Ek sample sheet bhej kar dekhta hai ki key / gateway / model sahi hai (1 paid call).</small>
</div>'''
        content = f'''
{alert}
<div class="form-section">
  <h5><i class="bi bi-camera"></i> Quotation sheet ki photo upload karein</h5>
  <p class="text-muted mb-2">Printed sheet par haath se kiye badlav (kati hui rate, upar likhi nayi rate,
  delivery, payment) AI padhega. Aapko har value check karke confirm karni hogi — rate, quantity aur
  delivery par alag se tick lagana zaroori hai.</p>
  <form method="post" action="/photo-quote/upload" enctype="multipart/form-data"
        onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').innerText='Reading photo… (30-60 sec)';">
    <input type="hidden" name="csrf_token" value="{esc(csrf)}">
    <div class="d-flex gap-2 flex-wrap align-items-center">
      <input type="file" name="photo" accept="image/jpeg,image/png,image/webp" capture="environment"
             class="form-control" style="max-width:380px" required {"disabled" if not ok else ""}>
      <button class="btn btn-primary" {"disabled" if not ok else ""}><i class="bi bi-magic"></i> Read photo</button>
    </div>
    <small class="text-muted">Tips: poora page frame mein, seedha upar se, achhi roshni. JPG/PNG, 20 MB tak.</small>
  </form>
</div>
{ai_box}
<div class="form-section mt-4">
  <h5>Recent</h5>
  <div class="table-responsive"><table class="table table-sm mb-0">
    <thead><tr><th>#</th><th>File</th><th>By</th><th>Uploaded</th><th>Result</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</div>'''
        return self._layout('Photo Quote', 'Photo Quote', content, 'photo')

    def handle_ai_test(self, form, user_id):
        if not self._is_admin(user_id) or not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*redirect('/photo-quote'))
        try:
            r = extractor.test_connection()
        except extractor.ExtractionError as e:
            return self.send(*html_response(self.pg_photo_quote(user_id, 'AI test FAILED: ' + e.message, err=True)))
        msg = (f"AI test OK ✅ — model {r['model_reported']} via {r['base_url']} ({r['mode']} mode). "
               f"Sample se padha: rate {r['rate_per_mtr']}, qty {r['quantity_mtrs']}, "
               f"delivery {r['delivery_weeks']} weeks, {r['needs_review']} doubts flagged.")
        return self.send(*html_response(self.pg_photo_quote(user_id, msg)))

    def handle_photo_upload(self, form, user_id):
        def fail(message):
            return self.send(*html_response(self.pg_photo_quote(user_id, message, err=True), 400))

        if not consume_csrf_token(form.get('csrf_token', '')):
            return fail('Session expire ho gaya — photo dobara choose karein.')
        up = form.get('photo')
        if not isinstance(up, UploadedFile) or not up.data:
            return fail('Photo choose karein.')
        if len(up.data) > extractor.MAX_UPLOAD_BYTES:
            return fail('Photo 20 MB se badi hai.')
        media_type = extractor.sniff_media_type(up.data)
        if media_type is None:
            return fail('Sirf JPG / PNG / WebP photo chalegi (iPhone HEIC ko JPG mein share karein).')
        store.ensure_schema()
        db = get_db()
        try:
            n = db.execute("SELECT COUNT(*) FROM vision_extractions WHERE created_by=? "
                           "AND created_at > datetime('now','-1 hour')", (user_id,)).fetchone()[0]
        finally:
            db.close()
        if n >= MAX_PER_HOUR:
            return fail(f'Ek ghante mein {MAX_PER_HOUR} photo ki limit poori ho gayi. Thodi der baad try karein.')

        rid = store.create(user_id, up.filename, media_type, up.data)
        self._run_extraction(rid, up.data)
        return self.send(*redirect(f'/photo-quote/{rid}'))

    def _run_extraction(self, rid, data):
        try:
            result = self._vision_extract(data)
        except extractor.ExtractionError as e:
            store.save_error(rid, e.message, e.raw_text)
        else:
            store.save_result(rid, result)

    def handle_photo_retry(self, rid, form, user_id):
        row = store.get(rid)
        if row is None or not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*redirect('/photo-quote'))
        if row['status'] != store.STATUS_CONFIRMED:
            if row.get('source') == 'text':
                try:
                    store.save_result(rid, extractor.extract_text(row.get('source_text') or ''))
                except extractor.ExtractionError as e:
                    store.save_error(rid, e.message, e.raw_text)
            else:
                data = store.read_image(row)
                if data:
                    self._run_extraction(rid, data)
        return self.send(*redirect(f'/photo-quote/{rid}'))

    def handle_photo_delete(self, rid, form, user_id):
        row = store.get(rid)
        if row and consume_csrf_token(form.get('csrf_token', '')) and \
                (row['created_by'] == user_id or self._is_admin(user_id)) and not row['quotation_id']:
            store.delete(rid)
        return self.send(*redirect('/photo-quote'))

    def serve_photo_image(self, rid):
        row = store.get(rid)
        data = store.read_image(row) if row else None
        if data is None:
            return self.send(*html_response(self.pg_404(), 404))
        return self.send(*file_response(data, row['stored_filename'], row['media_type'] or 'image/jpeg'))

    # ── review / confirm ─────────────────────────────────────
    def pg_photo_review(self, user_id, rid, values=None, errors=None, ticked=None, customer_id=None):
        row = store.get(rid)
        if row is None:
            return self.pg_404()
        if row['quotation_id']:
            values = row['confirmed'] or row['extracted']
        data = values or row['extracted'] or empty_extraction()
        review = {}
        for item in (row['extracted'] or {}).get('needs_review') or []:
            review.setdefault(item['field'], []).append(item['reason'])
        locked = bool(row['quotation_id'])
        csrf = new_csrf_token()
        ticked = ticked or set()

        top = ''
        if row['status'] == store.STATUS_FAILED:
            top += (f'<div class="alert alert-danger"><strong>Photo padhi nahi ja saki:</strong> '
                    f'{esc(row["error"] or "")}<br>Values haath se bhar sakte hain, ya "Read again" '
                    'dabayein.</div>')
        if errors:
            top += ('<div class="alert alert-danger"><strong>Quotation nahi bani:</strong><ul class="mb-0">'
                    + ''.join(f'<li>{esc(e)}</li>' for e in errors) + '</ul></div>')
        if locked:
            top += (f'<div class="alert alert-success">Confirmed — quotation '
                    f'<a href="/quotations/{row["quotation_id"]}">#{row["quotation_id"]}</a> ban chuki hai '
                    f'(<a href="/quotations/{row["quotation_id"]}/pdf">PDF</a>).</div>')
        flagged = [f for f in review if f not in ('',)]
        if flagged and not locked:
            top += ('<div class="alert alert-warning"><i class="bi bi-exclamation-triangle"></i> AI ko '
                    f'{len(flagged)} jagah shak hai — peeli boxes photo se zaroor milayein.</div>')

        rows_html = ''
        dis = 'disabled' if locked else ''
        for name, path, kind in _FIELDS:
            val = _fmt(_get(data, path))
            reasons = review.get(path, [])
            must = path in MUST_CONFIRM
            req = path in REQUIRED_FIELDS
            style = 'background:#FEF3C7;border:1px solid #F59E0B;' if reasons else ''
            label = esc(FIELD_LABELS.get(path, path)) + (' <span class="text-danger">*</span>' if req else '')
            if kind == 'textarea':
                inp = (f'<textarea name="{name}" class="form-control" rows="2" style="{style}" {dis}>'
                       f'{esc(val)}</textarea>')
            else:
                step = ' step="any" inputmode="decimal"' if kind == 'number' else ''
                strike = ('text-decoration:line-through;color:#6B7280;'
                          if path == 'original_rate_per_mtr' else '')
                inp = (f'<input name="{name}" value="{esc(val)}" class="form-control" type="{kind}"{step} '
                       f'style="{style}{strike}" {dis}>')
            extra = ''
            if reasons:
                extra += ''.join(f'<div class="small" style="color:#B45309"><i class="bi bi-eye"></i> '
                                 f'{esc(r)}</div>' for r in reasons)
            if must and not locked:
                chk = 'checked' if name in ticked else ''
                extra += (f'<label class="small mt-1 d-block" style="color:#B91C1C">'
                          f'<input type="checkbox" name="confirm_{name}" value="1" {chk} required> '
                          f'Maine photo se milaya — ye sahi hai</label>')
            rows_html += (f'<div class="mb-3"><label class="form-label mb-1"><strong>{label}</strong></label>'
                          f'{inp}{extra}</div>')

        customers = core.search_customers('', limit=1000)
        opts = '<option value="">— Customer choose karein —</option>' + ''.join(
            f'<option value="{c["id"]}" {"selected" if str(c["id"]) == str(customer_id or "") else ""}>'
            f'{esc(c["company_name"])}</option>' for c in customers)

        actions = ''
        if not locked:
            actions = f'''
<div class="mb-3"><label class="form-label mb-1"><strong>Customer <span class="text-danger">*</span></strong></label>
  <select name="customer_id" class="form-select" required>{opts}</select>
  <small class="text-muted">List mein nahi hai? <a href="/customers" target="_blank">Customer add karein</a>, phir page refresh.</small></div>
<button class="btn btn-success"><i class="bi bi-check2-circle"></i> Confirm &amp; create quotation</button>'''

        side_forms = ''
        if not locked:
            side_forms = f'''
<div class="d-flex gap-2 mt-2">
  <form method="post" action="/photo-quote/{rid}/retry" onsubmit="this.querySelector('button').disabled=true;">
    <input type="hidden" name="csrf_token" value="{esc(csrf)}">
    <button class="btn btn-sm btn-outline-secondary"><i class="bi bi-arrow-repeat"></i> Read again</button></form>
  <form method="post" action="/photo-quote/{rid}/delete" onsubmit="return confirm('Delete this photo?')">
    <input type="hidden" name="csrf_token" value="{esc(csrf)}">
    <button class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i> Delete</button></form>
</div>'''

        audit = (f'<small class="text-muted d-block mt-2">Model: {esc(row["model"] or "-")} · '
                 f'attempts: {esc(row["attempts"] or "-")} · SHA-256: {esc((row["image_sha256"] or "")[:16])}…'
                 f' · uploaded {esc(row["created_at"] or "")}</small>')

        if row.get('source') == 'text':
            source_html = ('<h6>Pasted text (Telegram)</h6><pre style="white-space:pre-wrap;max-height:70vh;'
                           'overflow:auto;background:#F9FAFB;padding:12px;border-radius:8px">'
                           f'{esc(row.get("source_text") or "")}</pre>')
        else:
            source_html = (f'<a href="/photo-quote/{rid}/image" target="_blank" title="Open full size">'
                           f'<img src="/photo-quote/{rid}/image" alt="Uploaded quotation sheet" '
                           'style="width:100%;height:auto;border-radius:8px;border:1px solid #E5E7EB"></a>'
                           '<small class="text-muted">Photo par click karke full size mein dekhein.</small>')
        content = f'''
{top}
<div class="row g-4">
  <div class="col-lg-6">
    <div class="form-section" style="position:sticky;top:80px">
{source_html}
      {side_forms}
      {audit}
    </div>
  </div>
  <div class="col-lg-6">
    <form method="post" action="/photo-quote/{rid}/confirm" class="form-section">
      <input type="hidden" name="csrf_token" value="{esc(csrf)}">
      <h5 class="mb-3">Extracted values — check &amp; correct</h5>
      {rows_html}
      {actions}
    </form>
  </div>
</div>'''
        return self._layout(f'Photo Quote #{rid}', f'Photo Quote #{rid}', content, 'photo')

    def handle_photo_confirm(self, rid, form, user_id):
        row = store.get(rid)
        if row is None:
            return self.send(*redirect('/photo-quote'))
        if row['quotation_id']:
            return self.send(*redirect(f'/quotations/{row["quotation_id"]}'))

        values = empty_extraction()
        values['needs_review'] = (row['extracted'] or {}).get('needs_review') or []
        errors = []
        for name, path, kind in _FIELDS:
            raw = (form.get(name) or '').strip()
            if kind == 'number':
                if raw == '':
                    v = None
                else:
                    v, _ = extractor._to_number(raw)
                    if v is None:
                        errors.append(f'{FIELD_LABELS[path]}: "{raw}" number nahi hai.')
            else:
                v = raw[:2000] or None
            _set(values, path, v)
        errors += confirm.check_values(values)
        ticked = {name for name, path, _ in _FIELDS
                  if path in MUST_CONFIRM and form.get(f'confirm_{name}') == '1'}
        for name, path, _ in _FIELDS:
            if path in MUST_CONFIRM and name not in ticked:
                errors.append(f'{FIELD_LABELS[path]}: photo se milakar tick lagayein.')
        try:
            customer_id = int(form.get('customer_id') or 0) or None
        except (TypeError, ValueError):
            customer_id = None
        if not customer_id:
            errors.append('Customer choose karein.')
        if not consume_csrf_token(form.get('csrf_token', '')):
            errors.append('Session expire ho gaya — dobara submit karein.')

        def again():
            return self.send(*html_response(self.pg_photo_review(
                user_id, rid, values=values, errors=errors, ticked=ticked, customer_id=customer_id), 400))

        if errors:
            return again()

        try:
            doc = confirm.create_quotation(rid, values, customer_id, user_id)
        except confirm.ConfirmError as e:
            errors.extend(e.errors)
            return again()
        return self.send(*redirect(f'/quotations/{doc["id"]}'))
