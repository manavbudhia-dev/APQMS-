import json
import math
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import pdf_response
from utils.http_utils import json_for_script
from database import get_db
from database import gen_proforma_no
from database import insert_with_generated_no, DocumentNumberConflictError
from app.generators.proforma_generator import build_proforma_pdf
from utils.dates import date_in_fy
from datetime import datetime


class ProformaSaveError(Exception):
    """Raised when a submitted proforma invoice can't be saved because
    one or more line items had unparseable data (bad qty/rate). Carries
    a list of human-readable per-row issues so the caller can show the
    user exactly what to fix instead of silently dropping rows."""
    def __init__(self, issues):
        self.issues = issues
        super().__init__('; '.join(issues))


class ProformaNumberConflictError(Exception):
    """Raised when a unique pi_no couldn't be generated after repeated
    collisions with concurrent requests."""


class ProformaPagesMixin:
    PROFORMA_STATUSES = ['Draft', 'Sent', 'Confirmed', 'Cancelled']
    PROFORMA_PAGE_SIZE = 10
    PNO_MAX_ATTEMPTS = 5

    def _insert_proforma_row(self, db, pno_fn, values_fn, max_attempts=None):
        """Insert a new `proforma_invoices` row, retrying if the generated
        pi_no collides with one another request just inserted.

        pno_fn: () -> str, generates a candidate pi_no.
        values_fn: (pno) -> full parameter tuple for the INSERT, in
            (pi_no,order_no,order_date,date,customer_id,subject,terms,
            status,notes,created_by) order.

        The read of MAX(pi_no) and the INSERT run inside one IMMEDIATE
        transaction (see database.insert_with_generated_no), so the write
        lock is held from before the read and a concurrent request cannot
        claim the same number in between. Returns (new_row_id, pi_no) with
        that transaction still open, so the caller can add the line items
        and commit the whole invoice as one unit. Raises
        ProformaNumberConflictError — never lets the underlying sqlite3
        error bubble up — if every attempt collides."""
        try:
            return insert_with_generated_no(
                db,
                "INSERT INTO proforma_invoices "
                "(pi_no,order_no,order_date,date,customer_id,subject,terms,status,notes,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                pno_fn, values_fn, max_attempts or self.PNO_MAX_ATTEMPTS)
        except DocumentNumberConflictError as e:
            raise ProformaNumberConflictError(str(e)) from e

    def handle_proforma_create(self, form, user_id):
        db = get_db()
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        order_no = form.get('order_no', '')
        order_date = form.get('order_date', '')
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        # A brand-new proforma invoice is normally a Draft, but the form
        # lets the status be set up front (e.g. straight to "Sent") —
        # only trust it if it's one of the recognised statuses.
        status = form.get('status', 'Draft')
        if status not in self.PROFORMA_STATUSES:
            status = 'Draft'

        try:
            pid, _ = self._insert_proforma_row(
                db, lambda: gen_proforma_no(db),
                lambda pno: (pno, order_no, order_date, date, customer_id,
                             '', '',
                             status, form.get('notes', ''), user_id))
        except ProformaNumberConflictError:
            db.rollback()
            db.close()
            return self._render_save_error(
                'Another proforma invoice was being created at the exact '
                'same moment, so a unique PI number could not be '
                'generated. Nothing was saved &mdash; please try '
                'submitting again.',
                '/proforma/new', active='proforma')

        try:
            self._save_proforma_items(db, pid, form)
        except ProformaSaveError as e:
            db.rollback()
            db.close()
            issues_html = '<br>'.join(esc(issue) for issue in e.issues)
            return self._render_save_error(
                'The proforma invoice was not saved because some items '
                f'had invalid data:<br>{issues_html}',
                '/proforma/new', active='proforma')

        db.commit()
        db.close()
        return self.send(*redirect(f'/proforma/{pid}'))


    def _save_proforma_items(self, db, pid, form):
        """Insert proforma_invoice_items rows from submitted form data.

        Deliberately does NOT re-look up items in the `items` catalog
        table by id. Each row's full snapshot (name, description, spec,
        unit, rate) is submitted directly from the browser (hidden
        fields kept in sync with the in-page item list), so this works
        even when item_id is blank/NULL or points at a catalog item
        that's since been deleted or edited — which happens whenever a
        proforma invoice is duplicated/edited later. Re-deriving those
        fields from a live `items` lookup instead would silently drop
        any such row (lookup finds nothing -> item skipped), which is
        what previously caused items to vanish after Update/Repeat."""
        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        names = form.getlist('item_name[]')
        descriptions = form.getlist('description[]')
        specifications = form.getlist('specification[]')
        units = form.getlist('unit[]')
        rates = form.getlist('rate[]')

        issues = []
        rows = []
        for i, (iid, qty, name, desc, spec, unit, rate) in enumerate(
                zip(item_ids, quantities, names, descriptions,
                    specifications, units, rates)):
            if not name or not qty:
                continue

            qty_val = self._parse_numeric_field(qty)
            if qty_val is None:
                issues.append(
                    f'Row {i + 1} ("{name}"): quantity "{qty}" is not a '
                    f'valid number.')
                continue

            rate_val = 0.0
            if rate:
                rate_val = self._parse_numeric_field(rate)
                if rate_val is None:
                    issues.append(
                        f'Row {i + 1} ("{name}"): rate "{rate}" is not a '
                        f'valid number.')
                    continue

            try:
                iid_val = int(iid) if iid and iid != 'null' else None
            except (TypeError, ValueError):
                iid_val = None

            rows.append((pid, iid_val, name, desc, spec, qty_val, unit,
                         rate_val, 18, i))

        # Fail the whole save rather than silently dropping the bad
        # row(s) — the caller rolls back so no items (good or bad) end
        # up half-saved, and shows these messages to the user.
        if issues:
            raise ProformaSaveError(issues)

        for row in rows:
            db.execute(
                "INSERT INTO proforma_invoice_items "
                "(proforma_id,item_id,item_name,description,specification,"
                "quantity,unit,rate,gst_percent,sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                row)

    def handle_proforma_edit(self, pid, form, user_id):
        db = get_db()
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.send(*redirect('/proforma'))

        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        order_no = form.get('order_no', '')
        order_date = form.get('order_date', '')
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        status = form.get('status', p['status'])
        if status not in self.PROFORMA_STATUSES:
            status = p['status']

        db.execute(
            "UPDATE proforma_invoices SET order_no=?, order_date=?, date=?, "
            "customer_id=?, subject=?, terms=?, status=?, notes=? WHERE id=?",
            (order_no, order_date, date, customer_id, '',
             '', status, form.get('notes', ''), pid))

        # Delete existing items
        db.execute("DELETE FROM proforma_invoice_items WHERE proforma_id=?", (pid,))

        # Add new items
        try:
            self._save_proforma_items(db, pid, form)
        except ProformaSaveError as e:
            # Nothing has been committed yet, so rolling back undoes
            # both the UPDATE above and the DELETE — the proforma
            # invoice is left exactly as it was before this request.
            db.rollback()
            db.close()
            issues_html = '<br>'.join(esc(issue) for issue in e.issues)
            return self._render_save_error(
                'The proforma invoice was not updated because some items '
                f'had invalid data:<br>{issues_html}',
                f'/proforma/{pid}/edit', active='proforma')

        db.commit()
        db.close()
        return self.send(*redirect(f'/proforma/{pid}'))

    def handle_proforma_duplicate(self, pid, user_id):
        """Create a brand-new proforma invoice that's a copy of an
        existing one — same customer, subject, terms, notes, order info
        and line items (name, description, spec, unit, rate, GST) — but
        with a fresh auto-generated PI number, today's date, and status
        reset to Draft. Sent to the edit page afterwards so quantities,
        rates or the customer can be tweaked before it's issued."""
        db = get_db()
        src = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not src:
            db.close()
            return self.send(*redirect('/proforma'))
        src_items = db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? ORDER BY sort_order",
            (pid,)).fetchall()

        date = datetime.now().strftime('%d - %m - %Y')
        try:
            new_id, _ = self._insert_proforma_row(
                db, lambda: gen_proforma_no(db),
                lambda pno: (pno, src['order_no'], src['order_date'], date,
                             src['customer_id'], src['subject'], src['terms'],
                             'Draft', src['notes'], user_id))
        except ProformaNumberConflictError:
            db.rollback()
            db.close()
            return self._render_save_error(
                'This proforma invoice could not be duplicated because '
                'another proforma invoice was being created at the exact '
                'same moment, so a unique PI number could not be '
                'generated. Please try again.',
                f'/proforma/{pid}', active='proforma')

        for it in src_items:
            db.execute(
                "INSERT INTO proforma_invoice_items "
                "(proforma_id,item_id,item_name,description,specification,"
                "quantity,unit,rate,gst_percent,sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_id, it['item_id'], it['item_name'], it['description'],
                 it['specification'], it['quantity'], it['unit'],
                 it['rate'], it['gst_percent'], it['sort_order']))
        db.commit()
        db.close()
        return self.send(*redirect(f'/proforma/{new_id}/edit'))


    def _gen_pi_pdf(self, pid):
        db = get_db()
        row = db.execute("SELECT * FROM proforma_invoices WHERE id=?",
                         (pid,)).fetchone()
        if not row:
            db.close()
            return pdf_response(b'%PDF-1.4\n% Not found', 'not-found.pdf')
        p = dict(row)
        c = None
        if p.get('customer_id'):
            crow = db.execute("SELECT * FROM customers WHERE id=?",
                              (p['customer_id'],)).fetchone()
            c = dict(crow) if crow else None
        items = [dict(r) for r in db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? "
            "ORDER BY sort_order", (pid,)).fetchall()]
        db.close()
        pdf = build_proforma_pdf(p, c, items)
        return pdf_response(pdf, f"{p['pi_no']}.pdf")


    # ── PAGE: PROFORMA INVOICES LIST ─────────────────────────
    def _proforma_rows_html(self, rows_data, start_index=1):
        """Render <tr> rows for a set of proforma invoice records
        (already fetched). `start_index` is the running row number of
        the first row on this page (i.e. the offset + 1), so numbering
        stays continuous across pages instead of restarting at 1 every
        time."""
        rows = ''
        for i, p in enumerate(rows_data):
            n = start_index + i
            customer_label = p['company_name'] or p['contact_person'] or '-'
            rows += (f'<tr>'
                     f'<td><input type="checkbox" class="form-check-input q-row-check" value="{p["id"]}"></td>'
                     f'<td class="text-muted">{n}</td>'
                     f'<td><strong>{esc(p["pi_no"])}</strong></td>'
                     f'<td>{esc(p["order_no"] or "-")}</td>'
                     f'<td>{esc(p["date"])}</td>'
                     f'<td>{esc(customer_label)}</td>'
                     f'<td><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td>'
                     f'<td class="text-end">'
                     f'<a href="/proforma/{p["id"]}" class="row-action-btn me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/proforma/{p["id"]}/pdf" class="row-action-btn me-1 ra-pdf" title="Download PDF"><i class="bi bi-file-earmark-pdf"></i></a>'
                     f'<a href="/proforma/{p["id"]}/edit" class="row-action-btn me-1 ra-edit" title="Edit"><i class="bi bi-pencil"></i></a>'
                     f'<form method="POST" action="/proforma/{p["id"]}/delete" class="d-inline" '
                     f'onsubmit="return confirm(\'Delete proforma invoice {esc(p["pi_no"])} permanently?\')">'
                     f'<button type="submit" class="row-action-btn ra-delete" title="Delete"><i class="bi bi-trash"></i></button></form>'
                     f'</td></tr>')
        return rows

    def _proforma_empty_state(self, has_any, q, status, fy=None):
        """Illustration + message for zero results, whether the table
        is truly empty or just has no matches for the current
        search/filter (which now includes the selected financial year)."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-receipt"></i></div>'
                    '<h6>No proforma invoices yet</h6>'
                    '<p class="text-muted">Once you create a proforma invoice, it will show up here.</p>'
                    '<a href="/proforma/new" class="btn btn-primary btn-sm">'
                    '<i class="bi bi-plus-circle"></i> Create your first proforma invoice</a>'
                    '</div>')
        fy_note = f' in FY {esc(fy["label"])}' if fy is not None else ''
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching proforma invoices</h6>'
                '<p class="text-muted">Try a different search term'
                + (f' or clear the "{esc(status)}" filter' if status else '')
                + fy_note + '.'
                + (' Switch financial year in the dropdown at the top of the page to see other years.' if fy is not None else '')
                + '</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="piClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _proforma_pagination_html(self, page, total_pages, q, status):
        html = '<nav aria-label="Proforma invoices pagination"><ul class="pagination pagination-sm q-pagination mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="piGoToPage(event,{p})">{label}</a></li>')

        html += page_item(page - 1, '&laquo;', disabled=(page <= 1))
        # Windowed page numbers (max 5) around the current page
        start = max(1, page - 2)
        end = min(total_pages, start + 4)
        start = max(1, end - 4)
        if start > 1:
            html += page_item(1, '1')
            if start > 2:
                html += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>'
        for p in range(start, end + 1):
            html += page_item(p, str(p), active=(p == page))
        if end < total_pages:
            if end < total_pages - 1:
                html += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>'
            html += page_item(total_pages, str(total_pages))
        html += page_item(page + 1, '&raquo;', disabled=(page >= total_pages))
        html += '</ul></nav>'
        return html

    def _proforma_chips_html(self, status_counts, all_count, status):
        chips = [('', 'All', all_count)] + [(s, s, status_counts.get(s, 0)) for s in self.PROFORMA_STATUSES]
        html = ''
        for val, label, count in chips:
            active = ' active' if val == status else ''
            html += (f'<button type="button" class="q-chip{active}" data-status="{esc(val)}" '
                     f'onclick="piSetStatus(\'{esc(val)}\')">{esc(label)} '
                     f'<span class="q-chip-count">{count}</span></button>')
        return html

    def _proforma_stat_cards_html(self, status_counts, all_count):
        """Five summary cards (Total / Draft / Sent / Confirmed /
        Cancelled) shown above the proforma invoices table."""
        cards = [
            ('Total Proforma Invoices', all_count, 'bi-receipt', '#3B82F6', '#EFF6FF'),
            ('Draft', status_counts.get('Draft', 0), 'bi-file-earmark-richtext', '#D97706', '#FFFBEB'),
            ('Sent', status_counts.get('Sent', 0), 'bi-send', '#059669', '#ECFDF5'),
            ('Confirmed', status_counts.get('Confirmed', 0), 'bi-check-circle', '#7C3AED', '#F5F3FF'),
            ('Cancelled', status_counts.get('Cancelled', 0), 'bi-x-circle', '#DC2626', '#FEF2F2'),
        ]
        html = ''
        for label, count, icon, color, bg in cards:
            html += f'''
  <div class="col-lg col-md-4 col-6">
    <div class="stat-card q-stat-card">
      <div class="stat-icon" style="background:{bg};color:{color}"><i class="bi {icon}"></i></div>
      <div><div class="stat-value">{count}</div><div class="stat-label">{esc(label)}</div></div>
    </div>
  </div>'''
        return html

    def _proforma_fragment(self, q='', status='', page='1', page_size=None):
        """Fetch, filter, and paginate proforma invoices; return
        rendered HTML fragments used both for the initial page load and
        the JSON API that powers live search/filter/pagination without
        a full reload.

        Scoped to the session's currently-selected financial year (see
        BaseMixin._current_fy) unless "All Financial Years" is selected.
        The invoice date is free text, not a real SQL date column, so
        that part of the filtering happens in Python rather than SQL."""
        q = (q or '').strip()
        status = status or ''
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        if status not in self.PROFORMA_STATUSES:
            status = ''

        try:
            page_size = int(page_size) if page_size else self.PROFORMA_PAGE_SIZE
        except (TypeError, ValueError):
            page_size = self.PROFORMA_PAGE_SIZE
        if page_size not in (10, 25, 50, 100):
            page_size = self.PROFORMA_PAGE_SIZE

        fy = self._current_fy()

        db = get_db()

        search_where, search_params = [], []
        if q:
            like = f"%{q}%"
            search_where.append(
                "(p.pi_no LIKE ? OR p.order_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR p.date LIKE ?)")
            search_params.extend([like, like, like, like, like])
        search_sql = (" WHERE " + " AND ".join(search_where)) if search_where else ""

        # Whether the table has ANY data at all (ignoring filters) —
        # decides which empty-state message to show.
        has_any = db.execute("SELECT COUNT(*) c FROM proforma_invoices").fetchone()['c'] > 0

        # Every proforma invoice matching the search term (all statuses)
        # — the financial-year filter is applied here in Python, then
        # status counts/pagination are derived from this filtered set.
        search_rows = db.execute(
            f"SELECT p.*,c.company_name,c.contact_person FROM proforma_invoices p "
            f"LEFT JOIN customers c ON p.customer_id=c.id{search_sql} "
            f"ORDER BY p.created_at DESC", search_params).fetchall()
        db.close()
        search_rows = [r for r in search_rows if date_in_fy(r['date'], fy)]

        all_count = len(search_rows)
        status_counts = {s: 0 for s in self.PROFORMA_STATUSES}
        for r in search_rows:
            if r['status'] in status_counts:
                status_counts[r['status']] += 1

        filtered_rows = [r for r in search_rows if not status or r['status'] == status]

        total = len(filtered_rows)
        total_pages = max(1, math.ceil(total / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows_data = filtered_rows[offset:offset + page_size]

        if rows_data:
            rows_html = self._proforma_rows_html(rows_data, start_index=offset + 1)
        else:
            rows_html = f'<tr><td colspan="8" class="p-0">{self._proforma_empty_state(has_any, q, status, fy)}</td></tr>'

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + page_size, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total} proforma invoices'
                       if total else 'No proforma invoices found')

        return {
            'rows_html': rows_html,
            'pagination_html': self._proforma_pagination_html(page, total_pages, q, status),
            'chips_html': self._proforma_chips_html(status_counts, all_count, status),
            'stat_cards_html': self._proforma_stat_cards_html(status_counts, all_count),
            'count_label': count_label,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        }

    def pg_pilist(self, q='', status='', page='1', page_size=None):
        frag = self._proforma_fragment(q=q, status=status, page=page, page_size=page_size)
        page_size_val = frag['page_size']
        size_opts = ''.join(
            f'<option value="{s}"{" selected" if s == page_size_val else ""}>{s} / page</option>'
            for s in (10, 25, 50, 100))
        content = f'''
<style>
.q-page-head {{ display:flex; flex-wrap:wrap; gap:14px; align-items:center; justify-content:space-between; margin-bottom:20px; }}
.q-page-head-left {{ display:flex; gap:14px; align-items:flex-start; }}
.q-page-head-icon {{
  width:46px; height:46px; border-radius:12px; background:var(--primary-bg); color:var(--primary);
  display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0;
}}
.q-page-head-left h1 {{ font-size:22px; font-weight:700; color:#0F172A; margin:0; }}
.q-page-head-left p {{ font-size:13px; color:#64748B; margin:2px 0 0; }}

.q-stat-card {{ display:flex; align-items:center; gap:14px; padding:18px; }}

.q-toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
.q-search-wrap {{ flex:1 1 260px; min-width:220px; }}
.q-date-btn {{ position:relative; overflow:hidden; white-space:nowrap; }}
.q-date-btn input[type=date] {{
  position:absolute; inset:0; opacity:0; width:100%; height:100%; cursor:pointer; border:none; padding:0;
}}
.q-chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
.q-chip {{
  border:1px solid #E2E8F0; background:#fff; color:#374151; border-radius:8px; padding:7px 14px;
  font-size:13px; font-weight:500; display:inline-flex; align-items:center; gap:7px; cursor:pointer;
  transition:all .15s; white-space:nowrap;
}}
.q-chip:hover {{ border-color:var(--primary); color:var(--primary); }}
.q-chip.active {{ background:var(--primary-bg); border-color:var(--primary); color:var(--primary); font-weight:600; }}
.q-chip[data-status=""].active {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-chip-count {{ background:rgba(0,0,0,.06); border-radius:999px; padding:0 7px; font-size:11.5px; min-width:18px; text-align:center; }}
.q-chip.active .q-chip-count {{ background:rgba(0,0,0,.08); }}
.q-chip[data-status=""].active .q-chip-count {{ background:rgba(255,255,255,.25); }}

.q-empty {{ text-align:center; padding:56px 20px; }}
.q-empty-icon {{ width:64px; height:64px; border-radius:50%; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:26px; margin:0 auto 16px; }}
.q-empty h6 {{ margin-bottom:6px; font-weight:600; color:#0F172A; }}
.q-count-label {{ font-size:13px; color:#64748B; }}
#proformaTbody {{ transition:opacity .1s; }}
#proformaTbody.q-loading {{ opacity:.4; }}

.q-th-sort {{ cursor:default; user-select:none; }}
.q-th-sort i {{ font-size:11px; margin-left:3px; opacity:.55; }}

.q-pagination {{ display:flex; gap:6px; margin:0; }}
.q-pagination .page-item {{ list-style:none; }}
.q-pagination .page-link {{
  border:1px solid #E2E8F0; color:#334155; border-radius:8px; padding:5px 11px; font-size:13px;
  display:flex; align-items:center; justify-content:center; min-width:32px; height:32px;
}}
.q-pagination .page-item.active .page-link {{ background:var(--primary); border-color:var(--primary); color:#fff; font-weight:700; }}
.q-pagination .page-item.disabled .page-link {{ color:#CBD5E1; }}

.q-pagesize-select {{ font-size:13px; border-radius:8px; padding:5px 10px; border:1px solid #E2E8F0; color:#334155; }}

.q-tips-card {{ background:#EFF6FF; border:1px solid #DBEAFE; border-radius:var(--radius); padding:18px 22px; margin-top:20px; display:flex; gap:14px; }}
.q-tips-card .q-tips-icon {{
  width:34px; height:34px; border-radius:50%; background:#3B82F6; color:#fff; flex-shrink:0;
  display:flex; align-items:center; justify-content:center; font-size:15px;
}}
.q-tips-card h6 {{ font-weight:700; color:#0F172A; margin-bottom:6px; }}
.q-tips-card ul {{ margin:0; padding-left:18px; color:#334155; font-size:13px; }}
.q-tips-card li {{ margin-bottom:3px; }}
</style>

<div class="q-page-head">
  <div class="q-page-head-left">
    <div class="q-page-head-icon"><i class="bi bi-receipt"></i></div>
    <div>
      <h1>All Proforma Invoices</h1>
      <p>View and manage all customer proforma invoices</p>
    </div>
  </div>
  <a href="/proforma/new" class="btn btn-primary"><i class="bi bi-plus-circle"></i> New Proforma Invoice</a>
</div>

<div class="row g-3 mb-3" id="piStatCards">{frag["stat_cards_html"]}</div>

<div class="table-card">
  <div class="p-3 border-bottom">
    <div class="q-toolbar">
      <div class="input-group input-group-sm q-search-wrap">
        <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
        <input type="text" id="piSearchInput" class="form-control border-start-0" placeholder="Search by PI no., order no., customer or date..." value="{esc(q)}">
      </div>
      <button type="button" class="btn btn-outline-secondary btn-sm q-date-btn" title="Filter by date">
        <i class="bi bi-calendar3"></i> Select Date
        <input type="date" id="piDateInput" aria-label="Select date">
      </button>
      <div class="q-chips" id="piChips">{frag["chips_html"]}</div>
      <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" id="piExportBtn"><i class="bi bi-download"></i> Export</button>
    </div>
  </div>
  <div class="table-responsive-wrapper">
  <table class="table mb-0">
    <thead>
      <tr>
        <th style="width:36px"><input type="checkbox" class="form-check-input" id="piSelectAll"></th>
        <th style="width:40px">#</th>
        <th>PI No.</th>
        <th>Order No</th>
        <th class="q-th-sort">Date <i class="bi bi-arrow-down-up"></i></th>
        <th class="q-th-sort">Customer <i class="bi bi-arrow-down-up"></i></th>
        <th>Status</th>
        <th class="text-end">Actions</th>
      </tr>
    </thead>
    <tbody id="proformaTbody">{frag["rows_html"]}</tbody>
  </table>
  </div>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="piCountLabel">{frag["count_label"]}</span>
    <div class="d-flex align-items-center gap-2">
      <div id="piPagination">{frag["pagination_html"]}</div>
      <select class="q-pagesize-select" id="piPageSize">{size_opts}</select>
    </div>
  </div>
</div>

<div class="q-tips-card">
  <div class="q-tips-icon"><i class="bi bi-info-lg"></i></div>
  <div>
    <h6>Quick Tips</h6>
    <ul>
      <li>Create a new proforma invoice to send to your customer.</li>
      <li>Use filters to quickly find proforma invoices by status or date.</li>
      <li>You can view, edit or duplicate a proforma invoice from the actions menu.</li>
    </ul>
  </div>
</div>

<script>
(function() {{
  var state = {{
    q: {json_for_script(q)}, status: {json_for_script(status)},
    page: {frag["page"]}, pageSize: {frag["page_size"]}
  }};
  var debounceTimer = null;

  function refresh() {{
    var tbody = document.getElementById('proformaTbody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    params.set('page', state.page);
    params.set('page_size', state.pageSize);
    fetch('/api/proforma?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('piPagination').innerHTML = data.pagination_html;
        document.getElementById('piChips').innerHTML = data.chips_html;
        document.getElementById('piCountLabel').innerHTML = data.count_label;
        document.getElementById('piStatCards').innerHTML = data.stat_cards_html;
        document.getElementById('piSelectAll').checked = false;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.status) url.searchParams.set('status', state.status); else url.searchParams.delete('status');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        if (state.pageSize !== 10) url.searchParams.set('page_size', state.pageSize); else url.searchParams.delete('page_size');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.piGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.piSetStatus = function(s) {{
    state.status = s;
    state.page = 1;
    refresh();
  }};

  window.piClearFilters = function() {{
    state.q = '';
    state.status = '';
    state.page = 1;
    document.getElementById('piSearchInput').value = '';
    document.getElementById('piDateInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('piSearchInput');
  searchInput.addEventListener('input', function(e) {{
    clearTimeout(debounceTimer);
    var val = e.target.value;
    debounceTimer = setTimeout(function() {{
      state.q = val;
      state.page = 1;
      refresh();
    }}, 350);
  }});
  searchInput.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{
      e.preventDefault();
      clearTimeout(debounceTimer);
      state.q = e.target.value;
      state.page = 1;
      refresh();
    }}
  }});

  document.getElementById('piPageSize').addEventListener('change', function(e) {{
    state.pageSize = parseInt(e.target.value, 10) || 10;
    state.page = 1;
    refresh();
  }});

  var piDateInput = document.getElementById('piDateInput');
  piDateInput.addEventListener('click', function(e) {{
    // The native input is stretched invisibly over the whole button, but
    // browsers only auto-open the calendar when the click lands exactly on
    // the tiny native icon hotspot. Force it open on any click instead.
    if (typeof this.showPicker === 'function') {{
      try {{ this.showPicker(); }} catch (err) {{ /* ignore */ }}
    }}
  }});
  piDateInput.addEventListener('change', function(e) {{
    if (!e.target.value) return;
    var d = new Date(e.target.value + 'T00:00:00');
    var day = String(d.getDate()).padStart(2, '0');
    var month = String(d.getMonth() + 1).padStart(2, '0');
    var formatted = day + ' - ' + month + ' - ' + d.getFullYear();
    searchInput.value = formatted;
    state.q = formatted;
    state.page = 1;
    refresh();
  }});

  document.getElementById('piSelectAll').addEventListener('change', function(e) {{
    document.querySelectorAll('.q-row-check').forEach(function(cb) {{ cb.checked = e.target.checked; }});
  }});

  document.getElementById('piExportBtn').addEventListener('click', function() {{
    var rows = [['PI No.', 'Order No', 'Date', 'Customer', 'Status']];
    document.querySelectorAll('#proformaTbody tr').forEach(function(tr) {{
      var cells = tr.querySelectorAll('td');
      if (cells.length < 8) return;
      rows.push([
        cells[2].innerText.trim(), cells[3].innerText.trim(), cells[4].innerText.trim(),
        cells[5].innerText.trim(), cells[6].innerText.trim()
      ]);
    }});
    var csv = rows.map(function(r) {{
      return r.map(function(v) {{ return '"' + String(v).replace(/"/g, '""') + '"'; }}).join(',');
    }}).join('\\r\\n');
    var blob = new Blob([csv], {{ type: 'text/csv;charset=utf-8;' }});
    var link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'proforma_invoices.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }});
}})();
</script>'''
        return self._layout('Proforma Invoices', 'All Proforma Invoices', content, 'proforma')


    # ── PAGE: NEW PROFORMA INVOICE ────────────────────────────
    def pg_pinew(self):
        db = get_db()
        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        item_cat_rows = db.execute(
            "SELECT ic.item_id AS item_id, c.id AS cat_id, c.name AS cat_name "
            "FROM item_categories ic JOIN categories c ON ic.category_id = c.id").fetchall()
        pino = gen_proforma_no(db)
        today = datetime.now().strftime('%d - %m - %Y')
        db.close()

        # item_id -> [{id, name}, ...] — an item can sit in more than one
        # category, so the client-side category filter/column needs the
        # full list, not just a single id.
        item_cats = {}
        for r in item_cat_rows:
            item_cats.setdefault(r['item_id'], []).append(
                {'id': r['cat_id'], 'name': r['cat_name']})

        cust_opts = '<option value="">Select a customer...</option>'
        for c in customers:
            cust_opts += (f'<option value="{c["id"]}" '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        cat_opts = '<option value="">All Categories</option>'
        for cat in categories:
            cat_opts += f'<option value="{cat["id"]}">{esc(cat["name"])}</option>'
        cat_opts_item = cat_opts.replace('All Categories', 'No category (optional)', 1)

        common_units_pi = ['Mtr.', 'Pcs.', 'Kg.', 'Set', 'Box']
        existing_units_pi = sorted({i['unit'] for i in items if i['unit']})
        extra_units_pi = [u for u in existing_units_pi if u not in common_units_pi]
        unit_datalist_options_pi = ''.join(
            f'<option value="{esc(u)}">' for u in common_units_pi + extra_units_pi)

        status_opts = ''
        for s in self.PROFORMA_STATUSES:
            sel = ' selected' if s == 'Draft' else ''
            status_opts += f'<option value="{esc(s)}"{sel}>{esc(s)}</option>'

        # Whole item catalog (with its category tags) handed to the
        # browser once, so the "Add Item" picker can filter by category
        # and search text instantly without round-tripping to the
        # server for every keystroke. `</` is escaped so a stray item
        # name can never prematurely close the surrounding <script>.
        items_payload = [{
            'id': i['id'],
            'name': i['item_name'],
            'desc': i['description'] or '',
            'spec': i['specification'] or '',
            'unit': i['unit'],
            'rate': i['rate_per_unit'],
            'categories': item_cats.get(i['id'], []),
        } for i in items]
        items_json = json.dumps(items_payload).replace('</', '<\\/')

        js = f"""
let allItems = {items_json};
let piItems = [];
let itemCounter = 0;

function escapeHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function categoryLabel(item) {{
  return (item.categories && item.categories.length) ? item.categories[0].name : 'General';
}}

// Wires a hidden native <input type=date> (stacked over the calendar
// icon) to a visible "DD - MM - YYYY" text field: opening the icon
// shows the real date picker, and picking a date fills the text
// field in the format the rest of the app expects.
function wireDatePicker(pickerId, fieldId) {{
  const picker = document.getElementById(pickerId);
  const field = document.getElementById(fieldId);
  if (!picker || !field) return;
  const parts = field.value.trim().split('-').map(function(s) {{ return s.trim(); }});
  if (parts.length === 3 && parts[0].length === 2 && parts[2].length === 4) {{
    picker.value = parts[2] + '-' + parts[1] + '-' + parts[0];
  }}
  picker.addEventListener('change', function(e) {{
    if (!e.target.value) return;
    const d = new Date(e.target.value + 'T00:00:00');
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    field.value = day + ' - ' + month + ' - ' + year;
  }});
}}

function updateStatusDot() {{
  const sel = document.getElementById('statusSel');
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot status-dot-' + sel.value.toLowerCase();
}}

function fillCust() {{
  const sel = document.getElementById('custSel');
  const info = document.getElementById('custInfo');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cName').textContent = opt.dataset.name;
    document.getElementById('cCompany').textContent = opt.dataset.company;
    document.getElementById('cAddr').textContent = opt.dataset.address;
    info.style.display = 'block';
  }} else {{
    info.style.display = 'none';
  }}
  checkValid();
}}

// Rebuilds the "Item" <select> in the Add Item box to match whichever
// category and/or search text are currently set. Both are optional and
// combine (AND).
// Fills one item <select> from a list of catalog items, keeping the
// current selection if that item is still in the list. Used for both
// the "From Catalog" dropdown and the "Manual Entry" dropdown so they
// always show the same (filtered) set of database items.
function fillItemSelect(sel, list, showRate) {{
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Select an item...';
  sel.appendChild(placeholder);
  list.forEach(function(it) {{
    const opt = document.createElement('option');
    opt.value = it.id;
    opt.dataset.name = it.name;
    opt.dataset.desc = it.desc;
    opt.dataset.spec = it.spec;
    opt.dataset.unit = it.unit;
    opt.dataset.rate = it.rate;
    opt.dataset.cat = categoryLabel(it);
    opt.textContent = showRate
      ? it.name + ' - Rs. ' + Number(it.rate).toLocaleString('en-IN') + '/' + it.unit
      : it.name;
    sel.appendChild(opt);
  }});
  if ([...sel.options].some(function(o) {{ return o.value === prev; }})) sel.value = prev;
}}

function refreshItemOptions() {{
  const catId = document.getElementById('catSel').value;
  const q = document.getElementById('itemSearch').value.trim().toLowerCase();
  const list = allItems.filter(function(it) {{
    if (catId && !(it.categories || []).some(function(c) {{ return String(c.id) === catId; }})) return false;
    if (q && it.name.toLowerCase().indexOf(q) === -1) return false;
    return true;
  }});
  // Catalog mode shows the preset rate (it's the rate that gets used);
  // Manual Entry hides it because you type your own rate there.
  fillItemSelect(document.getElementById('itemSelect'), list, true);
  fillItemSelect(document.getElementById('manualItemSelect'), list, false);
}}

function addItemRow() {{
  const sel = document.getElementById('itemSelect');
  const qtyInput = document.getElementById('qtyInput');
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  itemCounter++;
  piItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: parseFloat(opt.dataset.rate),
    cat: opt.dataset.cat,
    qty: qty,
    manual: false
  }});
  sel.value = '';
  qtyInput.value = '1';
  renderItems();
}}

// Toggles the Add Item box between picking a catalog item (with its
// preset rate) and picking a catalog item but typing the
// rate by hand.
function setAddItemMode(mode) {{
  const isManual = mode === 'manual';
  document.getElementById('catalogAddPanel').style.display = isManual ? 'none' : '';
  document.getElementById('manualAddPanel').style.display = isManual ? '' : 'none';
  document.getElementById('modeBtnCatalog').classList.toggle('active', !isManual);
  document.getElementById('modeBtnManual').classList.toggle('active', isManual);
}}

// Shows the picked item's unit (read-only) in the Manual Entry panel.
function onManualItemChange() {{
  const sel = document.getElementById('manualItemSelect');
  const unitInput = document.getElementById('manualUnitInput');
  const opt = sel.value ? sel.options[sel.selectedIndex] : null;
  unitInput.value = opt ? (opt.dataset.unit || '') : '';
}}

// Adds a line for an item picked from the item database, but with the
// Rate typed by hand instead of using the catalog's preset rate. Name /
// description / specification / unit / category all come from the
// database item, so only quantity and rate are typed; the line amount
// is qty x rate, same as every other row.
function addManualItemRow() {{
  const sel = document.getElementById('manualItemSelect');
  const qtyInput = document.getElementById('manualQtyInput');
  const unitInput = document.getElementById('manualUnitInput');
  const rateInput = document.getElementById('manualRateInput');

  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const rate = parseFloat(rateInput.value);
  if (!rate || rate <= 0) {{ alert('Please enter a valid rate.'); return; }}
  const opt = sel.options[sel.selectedIndex];

  itemCounter++;
  piItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: rate,
    cat: opt.dataset.cat,
    qty: qty,
    manual: true
  }});
  sel.value = '';
  qtyInput.value = '1';
  unitInput.value = '';
  rateInput.value = '';
  renderItems();
}}

function removeItem(id) {{
  piItems = piItems.filter(function(i) {{ return i.id !== id; }});
  renderItems();
}}

function renderItems() {{
  const body = document.getElementById('itemsBody');
  document.getElementById('itemsCount').textContent = piItems.length;
  if (piItems.length === 0) {{
    body.innerHTML = '<tr class="empty-row"><td colspan="7">' +
      '<i class="bi bi-box"></i>' +
      '<div class="empty-title">No items added yet.</div>' +
      '<div class="empty-sub">Search and add items from your catalog to create this proforma invoice.</div>' +
      '</td></tr>';
    updSum();
    return;
  }}
  let rows = '';
  piItems.forEach(function(item, idx) {{
    const amt = item.qty * item.rate;
    const loc = {{maximumFractionDigits: 2}};
    rows += '<tr>' +
      '<td>' + (idx + 1) +
      '<input type="hidden" name="item_id[]" value="' + escapeHtml(item.itemId) + '">' +
      '<input type="hidden" name="item_name[]" value="' + escapeHtml(item.name) + '">' +
      '<input type="hidden" name="description[]" value="' + escapeHtml(item.desc) + '">' +
      '<input type="hidden" name="specification[]" value="' + escapeHtml(item.spec) + '">' +
      '<input type="hidden" name="unit[]" value="' + escapeHtml(item.unit) + '">' +
      '<input type="hidden" name="rate[]" value="' + item.rate + '">' +
      '<input type="hidden" name="quantity[]" value="' + item.qty + '">' +
      '</td>' +
      '<td>' + escapeHtml(item.name) + (item.manual ? ' <span class="badge bg-secondary" style="font-size:9px;vertical-align:middle">Manual</span>' : '') + '</td>' +
      '<td class="text-muted">' + escapeHtml(item.cat) + '</td>' +
      '<td>' + item.qty.toLocaleString('en-IN', loc) + ' ' + escapeHtml(item.unit) + '</td>' +
      '<td>Rs. ' + item.rate.toLocaleString('en-IN', {{maximumFractionDigits: 2}}) + '</td>' +
      '<td><strong>Rs. ' + amt.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</strong></td>' +
      '<td><button type="button" class="btn btn-sm btn-link text-danger p-0" onclick="removeItem(' + item.id + ')" title="Remove"><i class="bi bi-trash"></i></button></td>' +
      '</tr>';
  }});
  body.innerHTML = rows;
  updSum();
}}

function updSum() {{
  let subtotal = 0;
  piItems.forEach(function(item) {{ subtotal += item.qty * item.rate; }});
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  const loc = {{maximumFractionDigits: 0}};
  document.getElementById('sCnt').textContent = piItems.length;
  document.getElementById('sSub').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN', loc);
  document.getElementById('sGst').textContent = 'Rs. ' + gst.toLocaleString('en-IN', loc);
  document.getElementById('sGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN', loc);
  checkValid();
}}

function checkValid() {{
  const custOk = document.getElementById('custSel').value !== '';
  const itemsOk = piItems.length > 0;
  document.getElementById('validationHint').style.display =
    (custOk && itemsOk) ? 'none' : 'block';
}}

function submitNewCustomer() {{
  const contact = document.getElementById('newContact').value.trim();
  const company = document.getElementById('newCompany').value.trim();
  const err = document.getElementById('addCustError');
  err.style.display = 'none';
  if (!company) {{
    err.textContent = 'Company name is required.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    contact_person: contact, company_name: company,
    address: document.getElementById('newAddress').value,
    phone: document.getElementById('newPhone').value,
    email: document.getElementById('newEmail').value,
    gstin: document.getElementById('newGstin').value,
    status: document.getElementById('newStatus').value
  }};
  fetch('/api/customers/add', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, data: d}}; }}); }})
    .then(function(res) {{
      if (!res.ok || !res.data.id) throw new Error(res.data.error || 'failed');
      const sel = document.getElementById('custSel');
      const opt = document.createElement('option');
      opt.value = res.data.id;
      opt.dataset.name = res.data.contact_person;
      opt.dataset.company = res.data.company_name;
      opt.dataset.address = res.data.address || '';
      opt.textContent = res.data.company_name + ' (' + res.data.contact_person + ')';
      sel.appendChild(opt);
      sel.value = res.data.id;
      fillCust();
      ['newContact','newCompany','newAddress','newPhone','newEmail','newGstin'].forEach(function(id) {{
        document.getElementById(id).value = '';
      }});
      const modalEl = document.getElementById('addCustModal');
      (bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl)).hide();
    }})
    .catch(function(e) {{
      err.textContent = (e && e.message) || 'Could not add customer. Please try again.';
      err.style.display = 'block';
    }});
}}

function toggleBoldSelection(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var ta = wrap ? wrap.querySelector('textarea') : null;
  if (!ta) return;
  var start = ta.selectionStart, end = ta.selectionEnd;
  if (start === end) {{
    alert('Select the text you want to bold first.');
    return;
  }}
  var val = ta.value;
  var selected = val.slice(start, end);
  var before = val.slice(0, start);
  var after = val.slice(end);
  var alreadyBold = selected.length >= 4 && selected.startsWith('**') && selected.endsWith('**');
  var replacement = alreadyBold ? selected.slice(2, -2) : ('**' + selected + '**');
  ta.value = before + replacement + after;
  ta.focus();
  ta.selectionStart = start;
  ta.selectionEnd = start + replacement.length;
}}

function updateCounter(el, counterId) {{
  var counter = document.getElementById(counterId);
  if (!counter) return;
  var max = el.getAttribute('maxlength');
  var len = el.value.length;
  counter.textContent = len + '/' + max;
  counter.classList.toggle('near-limit', !!max && len > max * 0.85 && len < max);
  counter.classList.toggle('at-limit', !!max && len >= max);
}}

function formatPreviewText(text) {{
  var div = document.createElement('div');
  div.textContent = text || '';
  var escaped = div.innerHTML;
  var formatted = escaped.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>').replace(/\\n/g, '<br>');
  return formatted || '<span class="text-muted">Nothing to preview yet.</span>';
}}

function toggleDescPreview(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var iconWrap = wrap.querySelector('.field-icon-wrap');
  var ta = iconWrap.querySelector('textarea');
  var previewBox = wrap.querySelector('.field-preview-box');
  var showing = btn.classList.toggle('active');
  if (showing) {{
    previewBox.innerHTML = formatPreviewText(ta.value);
    iconWrap.classList.add('d-none');
    previewBox.classList.remove('d-none');
    btn.innerHTML = '<i class="bi bi-eye-slash"></i> Edit';
  }} else {{
    iconWrap.classList.remove('d-none');
    previewBox.classList.add('d-none');
    btn.innerHTML = '<i class="bi bi-eye"></i> Preview';
  }}
}}

function submitNewItem() {{
  const name = document.getElementById('newItemName').value.trim();
  const desc = document.getElementById('newItemDesc').value.trim();
  const unit = document.getElementById('newItemUnit').value.trim();
  const rateStr = document.getElementById('newItemRate').value.trim();
  const err = document.getElementById('addItemError');
  err.style.display = 'none';
  if (!name) {{
    err.textContent = 'Item name is required.';
    err.style.display = 'block';
    return;
  }}
  if (!desc) {{
    err.textContent = 'Description is required.';
    err.style.display = 'block';
    return;
  }}
  if (!unit) {{
    err.textContent = 'Unit is required.';
    err.style.display = 'block';
    return;
  }}
  const rate = parseFloat(rateStr);
  if (!rateStr || isNaN(rate) || rate < 0) {{
    err.textContent = 'Enter a valid rate.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    item_name: name,
    unit: unit,
    rate_per_unit: rate,
    description: document.getElementById('newItemDesc').value,
    specification: document.getElementById('newItemSpec').value
  }};
  fetch('/api/items/add', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, data: d}}; }}); }})
    .then(function(res) {{
      if (!res.ok || !res.data.id) throw new Error(res.data.error || 'failed');
      allItems.push({{
        id: res.data.id, name: res.data.name, desc: res.data.desc, spec: res.data.spec,
        unit: res.data.unit, rate: res.data.rate, categories: res.data.categories || []
      }});
      refreshItemOptions();
      document.getElementById('itemSelect').value = String(res.data.id);
      ['newItemName','newItemRate','newItemDesc','newItemSpec'].forEach(function(id) {{
        document.getElementById(id).value = '';
      }});
      document.getElementById('newItemUnit').value = 'Mtr.';
      updateCounter(document.getElementById('newItemDesc'), 'newItemDesc-counter');
      updateCounter(document.getElementById('newItemSpec'), 'newItemSpec-counter');
      const modalEl = document.getElementById('addItemModal');
      (bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl)).hide();
    }})
    .catch(function(e) {{
      err.textContent = (e && e.message) || 'Could not add item. Please try again.';
      err.style.display = 'block';
    }});
}}

function submitProformaForm() {{
  if (piItems.length === 0) {{
    alert('Please add at least one item to the proforma invoice.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{
  refreshItemOptions();
  renderItems();
  checkValid();
  wireDatePicker('pnDatePicker', 'pnDateField');
}});
"""

        content = f'''
<div class="page-head">
  <div class="page-head-left">
    <i class="bi bi-receipt"></i>
    <div>
      <h1>Create New Proforma Invoice</h1>
      <p>Create a new proforma invoice for your customer</p>
    </div>
  </div>
  <a href="/proforma" class="btn btn-outline-secondary"><i class="bi bi-arrow-left"></i> Back to Proforma Invoices</a>
</div>

<form method="POST" action="/proforma/new" id="piForm" onsubmit="return submitProformaForm()">
<div class="row g-3">
  <div class="col-lg-7">

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-receipt"></i>
        <div class="qc-head-text">
          <h3>Proforma Invoice Details</h3>
          <p>Basic information for this proforma invoice</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-3">
          <label class="form-label">PI No.</label>
          <input class="form-control" value="{esc(pino)}" readonly style="background:#F8FAFC">
          <small class="text-muted" style="font-size:11px">Auto-generated</small>
        </div>
        <div class="col-md-3">
          <label class="form-label">Order No.</label>
          <input name="order_no" class="form-control" placeholder="Customer order ref">
        </div>
        <div class="col-md-3">
          <label class="form-label">Order Date</label>
          <input type="date" name="order_date" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Date</label>
          <div class="input-icon date-icon">
            <i class="bi bi-calendar3"></i>
            <input type="date" class="date-icon-picker" id="pnDatePicker" tabindex="-1" aria-label="Pick a date">
            <input name="date" id="pnDateField" class="form-control" value="{esc(today)}">
          </div>
        </div>
      </div>
      <div class="row g-3 mt-1">
        <div class="col-md-4">
          <label class="form-label">Status</label>
          <div class="status-select-wrap">
            <span class="status-dot status-dot-draft" id="statusDot"></span>
            <select name="status" id="statusSel" class="form-select" onchange="updateStatusDot()">{status_opts}</select>
          </div>
        </div>
      </div>
      <div class="mt-3">
        <label class="form-label">Customer <span style="color:var(--primary)">*</span></label>
        <div class="d-flex gap-2 align-items-start">
          <div class="input-icon flex-grow-1">
            <i class="bi bi-person"></i>
            <select name="customer_id" class="form-select" id="custSel" onchange="fillCust()" required>{cust_opts}</select>
          </div>
          <button type="button" class="btn btn-outline-secondary text-nowrap" data-bs-toggle="modal" data-bs-target="#addCustModal"><i class="bi bi-plus-lg"></i> Add New Customer</button>
        </div>
        <div class="mt-2 p-3 bg-light rounded" id="custInfo" style="display:none">
          <div class="fw-bold" id="cName"></div>
          <div id="cCompany"></div>
          <small class="text-muted" id="cAddr" style="white-space:pre-line"></small>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-box-seam"></i>
        <div class="qc-head-text">
          <h3>Add Items</h3>
          <p>Select items and quantities for this proforma invoice</p>
        </div>
      </div>
      <hr class="qc-divider">
      <label class="form-label">Filter by Category</label>
      <div class="row g-2">
        <div class="col-md-6">
          <div class="input-icon">
            <i class="bi bi-grid"></i>
            <select class="form-select" id="catSel" onchange="refreshItemOptions()">{cat_opts}</select>
          </div>
        </div>
        <div class="col-md-6">
          <div class="input-icon">
            <i class="bi bi-search"></i>
            <input type="text" class="form-control" id="itemSearch" placeholder="Search items..." oninput="refreshItemOptions()">
          </div>
        </div>
      </div>
      <div class="field-hint-row">
        <small>Add items from the catalog or search directly, or use "New Item" above to add one on the spot. Enter quantity for each item and add to the list.</small>
        <a href="/categories"><i class="bi bi-gear"></i> Manage categories</a>
      </div>

      <div class="add-item-box">
        <div class="d-flex justify-content-between align-items-center">
          <div class="box-label">Add Item</div>
          <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-toggle="modal" data-bs-target="#addItemModal"><i class="bi bi-plus-lg"></i> New Item</button>
        </div>
        <div class="add-item-mode-toggle">
          <button type="button" id="modeBtnCatalog" class="active" onclick="setAddItemMode('catalog')">From Catalog</button>
          <button type="button" id="modeBtnManual" onclick="setAddItemMode('manual')">Manual Entry</button>
        </div>

        <div id="catalogAddPanel" class="row g-2 align-items-end">
          <div class="col-md-7">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="itemSelect"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="qtyInput" min="0.01" step="0.01" value="1" data-enter-action="addItemRow()">
          </div>
          <div class="col-md-3">
            <button type="button" class="btn btn-primary w-100" onclick="addItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
          </div>
        </div>

        <div id="manualAddPanel" class="row g-2 align-items-end" style="display:none">
          <div class="col-md-5">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="manualItemSelect" onchange="onManualItemChange()"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity</label>
            <input type="number" class="form-control" id="manualQtyInput" min="0.01" step="0.01" value="1" data-enter-action="addManualItemRow()">
          </div>
          <div class="col-md-2">
            <label class="form-label">Unit</label>
            <input type="text" class="form-control" id="manualUnitInput" placeholder="-" readonly tabindex="-1">
          </div>
          <div class="col-md-3">
            <label class="form-label">Rate (Rs.) <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="manualRateInput" min="0.01" step="0.01" placeholder="Rate per unit" data-enter-action="addManualItemRow()">
          </div>
          <div class="col-12">
            <button type="button" class="btn btn-primary w-100" onclick="addManualItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
            <div class="add-item-manual-hint">Pick the item from your catalog and type only the rate &mdash; the catalog rate isn't used; the line amount is quantity &times; your rate.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-list-ul"></i>
        <div class="qc-head-text">
          <h3>Items in Proforma Invoice (<span id="itemsCount">0</span>)</h3>
          <p>Items you've added will appear here</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="items-table-wrap">
        <table class="items-table">
          <thead>
            <tr>
              <th>#</th><th>ITEM NAME</th><th>CATEGORY</th><th>QUANTITY</th>
              <th>RATE (Rs.)</th><th>AMOUNT (Rs.)</th><th>ACTION</th>
            </tr>
          </thead>
          <tbody id="itemsBody">
            <tr class="empty-row"><td colspan="7">
              <i class="bi bi-box"></i>
              <div class="empty-title">No items added yet.</div>
              <div class="empty-sub">Search and add items from your catalog to create this proforma invoice.</div>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Notes</h3>
          <p>Add any additional internal notes</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-12">
          <label class="form-label">Notes (Internal)</label>
          <textarea name="notes" class="form-control" rows="2" placeholder="Optional internal notes (not visible to customer)"></textarea>
        </div>
      </div>
    </div>

  </div>

  <div class="col-lg-5">
    <div class="form-section" style="position:sticky;top:86px">
      <div class="qc-head">
        <i class="bi bi-bar-chart"></i>
        <div class="qc-head-text">
          <h3>Proforma Invoice Summary</h3>
          <p>Summary of items and charges</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="total-line"><span>Items:</span><strong id="sCnt">0</strong></div>
      <div class="total-line"><span>Sub-Total:</span><strong id="sSub">Rs. 0</strong></div>
      <div class="total-line"><span>GST (18%):</span><strong id="sGst">Rs. 0</strong></div>
      <div class="grand-total-box"><span>Grand Total:</span><strong id="sGrand">Rs. 0</strong></div>

      <button type="submit" class="btn btn-primary w-100 btn-lg mt-3" id="submitBtn" title="Save (Ctrl+S)"><i class="bi bi-file-earmark-check"></i> Create Proforma Invoice</button>

      <div class="info-box">
        <i class="bi bi-info-circle-fill"></i>
        <div>
          <strong>PDF will be available after creation</strong>
          <p id="validationHint">Add a customer and at least one item to continue.</p>
        </div>
      </div>
    </div>

    <div class="form-section tips-card">
      <div class="qc-head icon-amber">
        <i class="bi bi-lightbulb"></i>
        <div class="qc-head-text"><h3>Quick Tips</h3></div>
      </div>
      <div class="tip-row tip-pending"><i class="bi bi-check-circle"></i> Select the correct customer</div>
      <div class="tip-row tip-done"><i class="bi bi-check-circle-fill"></i> Add at least one item</div>
      <div class="tip-row tip-done"><i class="bi bi-check-circle-fill"></i> You can save as draft and continue later</div>
    </div>
  </div>
</div>
</form>

<div class="modal fade" id="addCustModal" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <div class="modal-header">
      <h5 class="modal-title">Add New Customer</h5>
      <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
    </div>
    <div class="modal-body">
      <div id="addCustError" class="alert alert-danger py-2" style="display:none"></div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">Company Name <span class="text-danger">*</span></label>
          <input id="newCompany" class="form-control" required data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Contact Person</label>
          <input id="newContact" class="form-control" data-enter-action="submitNewCustomer()"></div>
      </div>
      <div class="mb-2"><label class="form-label">Address</label>
        <textarea id="newAddress" class="form-control" rows="4" placeholder="e.g. STREET NO.1, ANAND NAGAR&#10;NEAR KIDZ NEST SCHOOL, MAKSUDAN&#10;JALANDHAR (PUNJAB), INDIA&#10;PIN CODE - 144008"></textarea></div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">Phone</label>
          <input id="newPhone" class="form-control" data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Email</label>
          <input id="newEmail" class="form-control" data-enter-action="submitNewCustomer()"></div>
      </div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">GSTIN</label>
          <input id="newGstin" class="form-control" data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Status</label>
          <select id="newStatus" class="form-select">
            <option value="active" selected>Active</option>
            <option value="inactive">Inactive</option>
          </select></div>
      </div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-primary" onclick="submitNewCustomer()"><i class="bi bi-plus-lg"></i> Add Customer</button>
    </div>
  </div></div>
</div>

<div class="modal fade" id="addItemModal" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <div class="modal-icon-header">
      <div class="mih-left">
        <div class="mih-icon"><i class="bi bi-box-seam"></i></div>
        <div>
          <h5 class="mih-title">Add New Item</h5>
          <div class="mih-subtitle">Add a new item to your catalog. This item will be available in quotations and invoices.</div>
        </div>
      </div>
      <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
    </div>
    <div class="modal-body">
      <div id="addItemError" class="alert alert-danger py-2" style="display:none"></div>

      <div class="mb-3">
        <label class="form-label">Item Name<span class="required-mark">*</span></label>
        <div class="field-icon-wrap">
          <i class="bi bi-box field-icon"></i>
          <input id="newItemName" class="form-control" required placeholder="Enter item name" data-enter-action="submitNewItem()">
        </div>
        <div class="field-hint">e.g. Sugar Elevator Chain 66.60 MM Pitch</div>
      </div>

      <div class="mb-3 desc-field-wrap">
        <div class="field-label-row">
          <label class="form-label">Description<span class="required-mark">*</span></label>
          <div class="field-actions">
            <button type="button" class="btn btn-outline-secondary desc-bold-btn" onclick="toggleBoldSelection(this)" title="Bold the selected text">B</button>
            <button type="button" class="btn btn-outline-secondary desc-preview-btn" onclick="toggleDescPreview(this)" title="Preview formatted text"><i class="bi bi-eye"></i> Preview</button>
          </div>
        </div>
        <div class="field-icon-wrap">
          <i class="bi bi-file-earmark-text field-icon"></i>
          <textarea id="newItemDesc" class="form-control" rows="3" maxlength="500" required
            placeholder="Enter item description" oninput="updateCounter(this,'newItemDesc-counter')"></textarea>
        </div>
        <div class="field-preview-box d-none"></div>
        <div class="field-hint-row">
          <div class="field-hint">You can select text and click <strong>Bold</strong> (or use **text**) to make it bold.</div>
          <div class="field-counter" id="newItemDesc-counter">0/500</div>
        </div>
      </div>

      <div class="mb-3">
        <label class="form-label">Specification <span class="text-muted fw-normal">(Optional)</span></label>
        <div class="field-icon-wrap">
          <i class="bi bi-gear field-icon"></i>
          <textarea id="newItemSpec" class="form-control" rows="4" maxlength="500"
            placeholder="Enter technical specification, material, size, etc." oninput="updateCounter(this,'newItemSpec-counter')"></textarea>
        </div>
        <div class="field-hint-row">
          <div class="field-hint">e.g. Material: SS304, Pitch: 66.60 MM, Breaking Load: 120 kN</div>
          <div class="field-counter" id="newItemSpec-counter">0/500</div>
        </div>
      </div>

      <div class="row">
        <div class="col-md-6 mb-3">
          <label class="form-label">Unit<span class="required-mark">*</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-tag field-icon"></i>
            <input id="newItemUnit" class="form-control" list="unitOptionsPI" value="Mtr." autocomplete="off" required data-enter-action="submitNewItem()">
          </div>
          <datalist id="unitOptionsPI">{unit_datalist_options_pi}</datalist>
          <div class="field-hint">e.g. Mtr., Pcs., Kg., Set, Box</div>
        </div>
        <div class="col-md-6 mb-3">
          <label class="form-label">Rate (Rs.)<span class="required-mark">*</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-currency-rupee field-icon"></i>
            <input id="newItemRate" type="number" class="form-control" step="0.01" min="0" placeholder="Enter rate" required data-enter-action="submitNewItem()">
          </div>
          <div class="field-hint">Enter the selling price per unit (in INR)</div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <div class="modal-footer-split">
        <div class="info-tip-box">
          <i class="bi bi-info-circle-fill"></i>
          <div class="info-tip-text"><strong>Tip:</strong> Keep item details clear and accurate. These will be used in quotations and invoices.</div>
        </div>
        <div class="footer-actions">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" onclick="submitNewItem()"><i class="bi bi-plus-lg"></i> Add Item</button>
        </div>
      </div>
    </div>
  </div></div>
</div>


<script>
{js}
</script>'''
        return self._layout('New Proforma Invoice', 'Create New Proforma Invoice', content, 'proforma')


    # ── PAGE: EDIT PROFORMA INVOICE ──────────────────────────
    def pg_piedit(self, pid):
        db = get_db()
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()

        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        item_cat_rows = db.execute(
            "SELECT ic.item_id AS item_id, c.id AS cat_id, c.name AS cat_name "
            "FROM item_categories ic JOIN categories c ON ic.category_id = c.id").fetchall()
        pi_items = db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? ORDER BY sort_order",
            (pid,)).fetchall()
        db.close()

        item_cats = {}
        for r in item_cat_rows:
            item_cats.setdefault(r['item_id'], []).append(
                {'id': r['cat_id'], 'name': r['cat_name']})

        def _cat_label(item_id):
            cats = item_cats.get(item_id, [])
            return cats[0]['name'] if cats else 'General'

        cust_opts = '<option value="">Select a customer...</option>'
        for c in customers:
            sel = ' selected' if p['customer_id'] == c['id'] else ''
            cust_opts += (f'<option value="{c["id"]}"{sel} '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        cat_opts = '<option value="">All Categories</option>'
        for cat in categories:
            cat_opts += f'<option value="{cat["id"]}">{esc(cat["name"])}</option>'
        cat_opts_item = cat_opts.replace('All Categories', 'No category (optional)', 1)

        common_units_pi = ['Mtr.', 'Pcs.', 'Kg.', 'Set', 'Box']
        existing_units_pi = sorted({i['unit'] for i in items if i['unit']})
        extra_units_pi = [u for u in existing_units_pi if u not in common_units_pi]
        unit_datalist_options_pi = ''.join(
            f'<option value="{esc(u)}">' for u in common_units_pi + extra_units_pi)

        status_opts = ''
        for s in self.PROFORMA_STATUSES:
            sel = ' selected' if s == p['status'] else ''
            status_opts += f'<option value="{esc(s)}"{sel}>{esc(s)}</option>'

        items_payload = [{
            'id': i['id'],
            'name': i['item_name'],
            'desc': i['description'] or '',
            'spec': i['specification'] or '',
            'unit': i['unit'],
            'rate': i['rate_per_unit'],
            'categories': item_cats.get(i['id'], []),
        } for i in items]
        items_json = json.dumps(items_payload).replace('</', '<\\/')

        # Pre-populate items
        def _js_str(val):
            """Safely embed arbitrary text inside a JS double-quoted string
            literal. Order matters: backslashes must be escaped first, or
            the escaping of quotes/newlines would double up. Without this,
            fields containing raw newlines (e.g. multi-line descriptions)
            produce invalid JS ('unterminated string literal'), which
            silently breaks the entire inline <script> block and prevents
            existing items from ever being rendered on edit."""
            s = esc(val or '')
            s = s.replace(chr(92), chr(92) + chr(92))   # backslash -> \\
            s = s.replace(chr(34), chr(92) + chr(34))   # "        -> \"
            s = s.replace(chr(10), chr(92) + 'n')        # \n       -> \n
            s = s.replace(chr(13), chr(92) + 'r')        # \r       -> \r
            return s

        def _js_num(val):
            """Embed a numeric DB value in a JS literal safely. A NULL
            column comes through as Python None, which would otherwise
            be printed as the bare word `None` — not valid JS. That
            single bad token throws at the top of the <script> block
            and aborts the whole thing before any function (including
            renderItems/fillCust) is even defined, which is why a
            proforma invoice with one null rate/qty/item_id silently
            shows no items at all on the edit page."""
            return 'null' if val is None else val

        existing_items_js = '['
        for idx, it in enumerate(pi_items):
            if idx > 0:
                existing_items_js += ','
            existing_items_js += '{'
            existing_items_js += f'id: {idx + 1}, '
            existing_items_js += f'itemId: {_js_num(it["item_id"])}, '
            existing_items_js += f'name: "{_js_str(it["item_name"])}", '
            existing_items_js += f'desc: "{_js_str(it["description"])}", '
            existing_items_js += f'spec: "{_js_str(it["specification"])}", '
            existing_items_js += f'unit: "{_js_str(it["unit"])}", '
            existing_items_js += f'rate: {_js_num(it["rate"])}, '
            existing_items_js += f'cat: "{_js_str(_cat_label(it["item_id"]))}", '
            existing_items_js += f'qty: {_js_num(it["quantity"])}'
            existing_items_js += '}'
        existing_items_js += ']'

        js = f"""
let allItems = {items_json};
let piItems = {existing_items_js};
let itemCounter = {len(pi_items)};

function escapeHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function categoryLabel(item) {{
  return (item.categories && item.categories.length) ? item.categories[0].name : 'General';
}}

// Wires a hidden native <input type=date> (stacked over the calendar
// icon) to a visible "DD - MM - YYYY" text field: opening the icon
// shows the real date picker, and picking a date fills the text
// field in the format the rest of the app expects.
function wireDatePicker(pickerId, fieldId) {{
  const picker = document.getElementById(pickerId);
  const field = document.getElementById(fieldId);
  if (!picker || !field) return;
  const parts = field.value.trim().split('-').map(function(s) {{ return s.trim(); }});
  if (parts.length === 3 && parts[0].length === 2 && parts[2].length === 4) {{
    picker.value = parts[2] + '-' + parts[1] + '-' + parts[0];
  }}
  picker.addEventListener('change', function(e) {{
    if (!e.target.value) return;
    const d = new Date(e.target.value + 'T00:00:00');
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    field.value = day + ' - ' + month + ' - ' + year;
  }});
}}

function updateStatusDot() {{
  const sel = document.getElementById('statusSel');
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot status-dot-' + sel.value.toLowerCase();
}}

function fillCust() {{
  const sel = document.getElementById('custSel');
  const info = document.getElementById('custInfo');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cName').textContent = opt.dataset.name;
    document.getElementById('cCompany').textContent = opt.dataset.company;
    document.getElementById('cAddr').textContent = opt.dataset.address;
    info.style.display = 'block';
  }} else {{
    info.style.display = 'none';
  }}
}}

// Rebuilds the "Item" <select> in the Add Item box to match whichever
// category and/or search text are currently set.
// Fills one item <select> from a list of catalog items, keeping the
// current selection if that item is still in the list. Used for both
// the "From Catalog" dropdown and the "Manual Entry" dropdown so they
// always show the same (filtered) set of database items.
function fillItemSelect(sel, list, showRate) {{
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Select an item...';
  sel.appendChild(placeholder);
  list.forEach(function(it) {{
    const opt = document.createElement('option');
    opt.value = it.id;
    opt.dataset.name = it.name;
    opt.dataset.desc = it.desc;
    opt.dataset.spec = it.spec;
    opt.dataset.unit = it.unit;
    opt.dataset.rate = it.rate;
    opt.dataset.cat = categoryLabel(it);
    opt.textContent = showRate
      ? it.name + ' - Rs. ' + Number(it.rate).toLocaleString('en-IN') + '/' + it.unit
      : it.name;
    sel.appendChild(opt);
  }});
  if ([...sel.options].some(function(o) {{ return o.value === prev; }})) sel.value = prev;
}}

function refreshItemOptions() {{
  const catId = document.getElementById('catSel').value;
  const q = document.getElementById('itemSearch').value.trim().toLowerCase();
  const list = allItems.filter(function(it) {{
    if (catId && !(it.categories || []).some(function(c) {{ return String(c.id) === catId; }})) return false;
    if (q && it.name.toLowerCase().indexOf(q) === -1) return false;
    return true;
  }});
  // Catalog mode shows the preset rate (it's the rate that gets used);
  // Manual Entry hides it because you type your own rate there.
  fillItemSelect(document.getElementById('itemSelect'), list, true);
  fillItemSelect(document.getElementById('manualItemSelect'), list, false);
}}

function addItemRow() {{
  const sel = document.getElementById('itemSelect');
  const qtyInput = document.getElementById('qtyInput');
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  itemCounter++;
  piItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: parseFloat(opt.dataset.rate),
    cat: opt.dataset.cat,
    qty: qty,
    manual: false
  }});
  sel.value = '';
  qtyInput.value = '1';
  renderItems();
}}

// Toggles the Add Item box between picking a catalog item (with its
// preset rate) and picking a catalog item but typing the
// rate by hand.
function setAddItemMode(mode) {{
  const isManual = mode === 'manual';
  document.getElementById('catalogAddPanel').style.display = isManual ? 'none' : '';
  document.getElementById('manualAddPanel').style.display = isManual ? '' : 'none';
  document.getElementById('modeBtnCatalog').classList.toggle('active', !isManual);
  document.getElementById('modeBtnManual').classList.toggle('active', isManual);
}}

// Shows the picked item's unit (read-only) in the Manual Entry panel.
function onManualItemChange() {{
  const sel = document.getElementById('manualItemSelect');
  const unitInput = document.getElementById('manualUnitInput');
  const opt = sel.value ? sel.options[sel.selectedIndex] : null;
  unitInput.value = opt ? (opt.dataset.unit || '') : '';
}}

// Adds a line for an item picked from the item database, but with the
// Rate typed by hand instead of using the catalog's preset rate. Name /
// description / specification / unit / category all come from the
// database item, so only quantity and rate are typed; the line amount
// is qty x rate, same as every other row.
function addManualItemRow() {{
  const sel = document.getElementById('manualItemSelect');
  const qtyInput = document.getElementById('manualQtyInput');
  const unitInput = document.getElementById('manualUnitInput');
  const rateInput = document.getElementById('manualRateInput');

  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const rate = parseFloat(rateInput.value);
  if (!rate || rate <= 0) {{ alert('Please enter a valid rate.'); return; }}
  const opt = sel.options[sel.selectedIndex];

  itemCounter++;
  piItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: rate,
    cat: opt.dataset.cat,
    qty: qty,
    manual: true
  }});
  sel.value = '';
  qtyInput.value = '1';
  unitInput.value = '';
  rateInput.value = '';
  renderItems();
}}

function removeItem(id) {{
  piItems = piItems.filter(function(i) {{ return i.id !== id; }});
  renderItems();
}}

function renderItems() {{
  const body = document.getElementById('itemsBody');
  document.getElementById('itemsCount').textContent = piItems.length;
  if (piItems.length === 0) {{
    body.innerHTML = '<tr class="empty-row"><td colspan="7">' +
      '<i class="bi bi-box"></i>' +
      '<div class="empty-title">No items added yet.</div>' +
      '<div class="empty-sub">Search and add items from your catalog to create this proforma invoice.</div>' +
      '</td></tr>';
    updSum();
    return;
  }}
  let rows = '';
  piItems.forEach(function(item, idx) {{
    const amt = item.qty * item.rate;
    const loc = {{maximumFractionDigits: 2}};
    rows += '<tr>' +
      '<td>' + (idx + 1) +
      '<input type="hidden" name="item_id[]" value="' + escapeHtml(item.itemId) + '">' +
      '<input type="hidden" name="item_name[]" value="' + escapeHtml(item.name) + '">' +
      '<input type="hidden" name="description[]" value="' + escapeHtml(item.desc) + '">' +
      '<input type="hidden" name="specification[]" value="' + escapeHtml(item.spec) + '">' +
      '<input type="hidden" name="unit[]" value="' + escapeHtml(item.unit) + '">' +
      '<input type="hidden" name="rate[]" value="' + item.rate + '">' +
      '<input type="hidden" name="quantity[]" value="' + item.qty + '">' +
      '</td>' +
      '<td>' + escapeHtml(item.name) + (item.manual ? ' <span class="badge bg-secondary" style="font-size:9px;vertical-align:middle">Manual</span>' : '') + '</td>' +
      '<td class="text-muted">' + escapeHtml(item.cat) + '</td>' +
      '<td>' + item.qty.toLocaleString('en-IN', loc) + ' ' + escapeHtml(item.unit) + '</td>' +
      '<td>Rs. ' + item.rate.toLocaleString('en-IN', {{maximumFractionDigits: 2}}) + '</td>' +
      '<td><strong>Rs. ' + amt.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</strong></td>' +
      '<td><button type="button" class="btn btn-sm btn-link text-danger p-0" onclick="removeItem(' + item.id + ')" title="Remove"><i class="bi bi-trash"></i></button></td>' +
      '</tr>';
  }});
  body.innerHTML = rows;
  updSum();
}}

function updSum() {{
  let subtotal = 0;
  piItems.forEach(function(item) {{ subtotal += item.qty * item.rate; }});
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  const loc = {{maximumFractionDigits: 0}};
  document.getElementById('sCnt').textContent = piItems.length;
  document.getElementById('sSub').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN', loc);
  document.getElementById('sGst').textContent = 'Rs. ' + gst.toLocaleString('en-IN', loc);
  document.getElementById('sGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN', loc);
}}

function submitNewCustomer() {{
  const contact = document.getElementById('newContact').value.trim();
  const company = document.getElementById('newCompany').value.trim();
  const err = document.getElementById('addCustError');
  err.style.display = 'none';
  if (!company) {{
    err.textContent = 'Company name is required.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    contact_person: contact, company_name: company,
    address: document.getElementById('newAddress').value,
    phone: document.getElementById('newPhone').value,
    email: document.getElementById('newEmail').value,
    gstin: document.getElementById('newGstin').value,
    status: document.getElementById('newStatus').value
  }};
  fetch('/api/customers/add', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, data: d}}; }}); }})
    .then(function(res) {{
      if (!res.ok || !res.data.id) throw new Error(res.data.error || 'failed');
      const sel = document.getElementById('custSel');
      const opt = document.createElement('option');
      opt.value = res.data.id;
      opt.dataset.name = res.data.contact_person;
      opt.dataset.company = res.data.company_name;
      opt.dataset.address = res.data.address || '';
      opt.textContent = res.data.company_name + ' (' + res.data.contact_person + ')';
      sel.appendChild(opt);
      sel.value = res.data.id;
      fillCust();
      ['newContact','newCompany','newAddress','newPhone','newEmail','newGstin'].forEach(function(id) {{
        document.getElementById(id).value = '';
      }});
      const modalEl = document.getElementById('addCustModal');
      (bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl)).hide();
    }})
    .catch(function(e) {{
      err.textContent = (e && e.message) || 'Could not add customer. Please try again.';
      err.style.display = 'block';
    }});
}}

function toggleBoldSelection(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var ta = wrap ? wrap.querySelector('textarea') : null;
  if (!ta) return;
  var start = ta.selectionStart, end = ta.selectionEnd;
  if (start === end) {{
    alert('Select the text you want to bold first.');
    return;
  }}
  var val = ta.value;
  var selected = val.slice(start, end);
  var before = val.slice(0, start);
  var after = val.slice(end);
  var alreadyBold = selected.length >= 4 && selected.startsWith('**') && selected.endsWith('**');
  var replacement = alreadyBold ? selected.slice(2, -2) : ('**' + selected + '**');
  ta.value = before + replacement + after;
  ta.focus();
  ta.selectionStart = start;
  ta.selectionEnd = start + replacement.length;
}}

function updateCounter(el, counterId) {{
  var counter = document.getElementById(counterId);
  if (!counter) return;
  var max = el.getAttribute('maxlength');
  var len = el.value.length;
  counter.textContent = len + '/' + max;
  counter.classList.toggle('near-limit', !!max && len > max * 0.85 && len < max);
  counter.classList.toggle('at-limit', !!max && len >= max);
}}

function formatPreviewText(text) {{
  var div = document.createElement('div');
  div.textContent = text || '';
  var escaped = div.innerHTML;
  var formatted = escaped.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>').replace(/\\n/g, '<br>');
  return formatted || '<span class="text-muted">Nothing to preview yet.</span>';
}}

function toggleDescPreview(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var iconWrap = wrap.querySelector('.field-icon-wrap');
  var ta = iconWrap.querySelector('textarea');
  var previewBox = wrap.querySelector('.field-preview-box');
  var showing = btn.classList.toggle('active');
  if (showing) {{
    previewBox.innerHTML = formatPreviewText(ta.value);
    iconWrap.classList.add('d-none');
    previewBox.classList.remove('d-none');
    btn.innerHTML = '<i class="bi bi-eye-slash"></i> Edit';
  }} else {{
    iconWrap.classList.remove('d-none');
    previewBox.classList.add('d-none');
    btn.innerHTML = '<i class="bi bi-eye"></i> Preview';
  }}
}}

function submitNewItem() {{
  const name = document.getElementById('newItemName').value.trim();
  const desc = document.getElementById('newItemDesc').value.trim();
  const unit = document.getElementById('newItemUnit').value.trim();
  const rateStr = document.getElementById('newItemRate').value.trim();
  const err = document.getElementById('addItemError');
  err.style.display = 'none';
  if (!name) {{
    err.textContent = 'Item name is required.';
    err.style.display = 'block';
    return;
  }}
  if (!desc) {{
    err.textContent = 'Description is required.';
    err.style.display = 'block';
    return;
  }}
  if (!unit) {{
    err.textContent = 'Unit is required.';
    err.style.display = 'block';
    return;
  }}
  const rate = parseFloat(rateStr);
  if (!rateStr || isNaN(rate) || rate < 0) {{
    err.textContent = 'Enter a valid rate.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    item_name: name,
    unit: unit,
    rate_per_unit: rate,
    description: document.getElementById('newItemDesc').value,
    specification: document.getElementById('newItemSpec').value
  }};
  fetch('/api/items/add', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, data: d}}; }}); }})
    .then(function(res) {{
      if (!res.ok || !res.data.id) throw new Error(res.data.error || 'failed');
      allItems.push({{
        id: res.data.id, name: res.data.name, desc: res.data.desc, spec: res.data.spec,
        unit: res.data.unit, rate: res.data.rate, categories: res.data.categories || []
      }});
      refreshItemOptions();
      document.getElementById('itemSelect').value = String(res.data.id);
      ['newItemName','newItemRate','newItemDesc','newItemSpec'].forEach(function(id) {{
        document.getElementById(id).value = '';
      }});
      document.getElementById('newItemUnit').value = 'Mtr.';
      updateCounter(document.getElementById('newItemDesc'), 'newItemDesc-counter');
      updateCounter(document.getElementById('newItemSpec'), 'newItemSpec-counter');
      const modalEl = document.getElementById('addItemModal');
      (bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl)).hide();
    }})
    .catch(function(e) {{
      err.textContent = (e && e.message) || 'Could not add item. Please try again.';
      err.style.display = 'block';
    }});
}}

function submitProformaForm() {{
  if (piItems.length === 0) {{
    alert('Please add at least one item to the proforma invoice.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{
  refreshItemOptions();
  renderItems();
  fillCust();
  wireDatePicker('peDatePicker', 'peDateField');
}});
"""

        content = f'''
<div class="page-head">
  <div class="page-head-left">
    <i class="bi bi-receipt"></i>
    <div>
      <h1>Edit Proforma Invoice</h1>
      <p>Update proforma invoice {esc(p["pi_no"])}</p>
    </div>
  </div>
  <a href="/proforma/{pid}" class="btn btn-outline-secondary"><i class="bi bi-arrow-left"></i> Back to Proforma Invoice</a>
</div>

<form method="POST" action="/proforma/{pid}/edit" id="piForm" onsubmit="return submitProformaForm()">
<div class="row g-3">
  <div class="col-lg-7">

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-receipt"></i>
        <div class="qc-head-text">
          <h3>Proforma Invoice Details</h3>
          <p>Basic information for this proforma invoice</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-3">
          <label class="form-label">PI No.</label>
          <input class="form-control" value="{esc(p['pi_no'])}" readonly style="background:#F8FAFC">
        </div>
        <div class="col-md-3">
          <label class="form-label">Order No.</label>
          <input name="order_no" class="form-control" value="{esc(p['order_no'] or '')}">
        </div>
        <div class="col-md-3">
          <label class="form-label">Order Date</label>
          <input type="date" name="order_date" class="form-control" value="{esc(p['order_date'] or '')}">
        </div>
        <div class="col-md-3">
          <label class="form-label">Date</label>
          <div class="input-icon date-icon">
            <i class="bi bi-calendar3"></i>
            <input type="date" class="date-icon-picker" id="peDatePicker" tabindex="-1" aria-label="Pick a date">
            <input name="date" id="peDateField" class="form-control" value="{esc(p['date'])}" required>
          </div>
        </div>
      </div>
      <div class="row g-3 mt-1">
        <div class="col-md-4">
          <label class="form-label">Status</label>
          <div class="status-select-wrap">
            <span class="status-dot status-dot-{esc(p['status'].lower())}" id="statusDot"></span>
            <select name="status" id="statusSel" class="form-select" onchange="updateStatusDot()">{status_opts}</select>
          </div>
        </div>
      </div>
      <div class="mt-3">
        <label class="form-label">Customer <span style="color:var(--primary)">*</span></label>
        <div class="d-flex gap-2 align-items-start">
          <div class="input-icon flex-grow-1">
            <i class="bi bi-person"></i>
            <select name="customer_id" class="form-select" id="custSel" onchange="fillCust()" required>{cust_opts}</select>
          </div>
          <button type="button" class="btn btn-outline-secondary text-nowrap" data-bs-toggle="modal" data-bs-target="#addCustModal"><i class="bi bi-plus-lg"></i> Add New Customer</button>
        </div>
        <div class="mt-2 p-3 bg-light rounded" id="custInfo" style="display:{'block' if p['customer_id'] else 'none'}">
          <div class="fw-bold" id="cName"></div>
          <div id="cCompany"></div>
          <small class="text-muted" id="cAddr" style="white-space:pre-line"></small>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-box-seam"></i>
        <div class="qc-head-text">
          <h3>Add Items</h3>
          <p>Select items and quantities for this proforma invoice</p>
        </div>
      </div>
      <hr class="qc-divider">
      <label class="form-label">Filter by Category</label>
      <div class="row g-2">
        <div class="col-md-6">
          <div class="input-icon">
            <i class="bi bi-grid"></i>
            <select class="form-select" id="catSel" onchange="refreshItemOptions()">{cat_opts}</select>
          </div>
        </div>
        <div class="col-md-6">
          <div class="input-icon">
            <i class="bi bi-search"></i>
            <input type="text" class="form-control" id="itemSearch" placeholder="Search items..." oninput="refreshItemOptions()">
          </div>
        </div>
      </div>
      <div class="field-hint-row">
        <small>Add items from the catalog or search directly, or use "New Item" above to add one on the spot. Enter quantity for each item and add to the list.</small>
        <a href="/categories"><i class="bi bi-gear"></i> Manage categories</a>
      </div>

      <div class="add-item-box">
        <div class="d-flex justify-content-between align-items-center">
          <div class="box-label">Add Item</div>
          <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-toggle="modal" data-bs-target="#addItemModal"><i class="bi bi-plus-lg"></i> New Item</button>
        </div>
        <div class="add-item-mode-toggle">
          <button type="button" id="modeBtnCatalog" class="active" onclick="setAddItemMode('catalog')">From Catalog</button>
          <button type="button" id="modeBtnManual" onclick="setAddItemMode('manual')">Manual Entry</button>
        </div>

        <div id="catalogAddPanel" class="row g-2 align-items-end">
          <div class="col-md-7">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="itemSelect"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="qtyInput" min="0.01" step="0.01" value="1" data-enter-action="addItemRow()">
          </div>
          <div class="col-md-3">
            <button type="button" class="btn btn-primary w-100" onclick="addItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
          </div>
        </div>

        <div id="manualAddPanel" class="row g-2 align-items-end" style="display:none">
          <div class="col-md-5">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="manualItemSelect" onchange="onManualItemChange()"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity</label>
            <input type="number" class="form-control" id="manualQtyInput" min="0.01" step="0.01" value="1" data-enter-action="addManualItemRow()">
          </div>
          <div class="col-md-2">
            <label class="form-label">Unit</label>
            <input type="text" class="form-control" id="manualUnitInput" placeholder="-" readonly tabindex="-1">
          </div>
          <div class="col-md-3">
            <label class="form-label">Rate (Rs.) <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="manualRateInput" min="0.01" step="0.01" placeholder="Rate per unit" data-enter-action="addManualItemRow()">
          </div>
          <div class="col-12">
            <button type="button" class="btn btn-primary w-100" onclick="addManualItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
            <div class="add-item-manual-hint">Pick the item from your catalog and type only the rate &mdash; the catalog rate isn't used; the line amount is quantity &times; your rate.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-list-ul"></i>
        <div class="qc-head-text">
          <h3>Items in Proforma Invoice (<span id="itemsCount">0</span>)</h3>
          <p>Items you've added will appear here</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="items-table-wrap">
        <table class="items-table">
          <thead>
            <tr>
              <th>#</th><th>ITEM NAME</th><th>CATEGORY</th><th>QUANTITY</th>
              <th>RATE (Rs.)</th><th>AMOUNT (Rs.)</th><th>ACTION</th>
            </tr>
          </thead>
          <tbody id="itemsBody">
            <tr class="empty-row"><td colspan="7">
              <i class="bi bi-box"></i>
              <div class="empty-title">No items added yet.</div>
              <div class="empty-sub">Search and add items from your catalog to create this proforma invoice.</div>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Notes</h3>
          <p>Add any additional internal notes</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-12">
          <label class="form-label">Notes (Internal)</label>
          <textarea name="notes" class="form-control" rows="2" placeholder="Optional internal notes (not visible to customer)">{esc(p['notes'] or '')}</textarea>
        </div>
      </div>
    </div>

  </div>

  <div class="col-lg-5">
    <div class="form-section" style="position:sticky;top:86px">
      <div class="qc-head">
        <i class="bi bi-bar-chart"></i>
        <div class="qc-head-text">
          <h3>Proforma Invoice Summary</h3>
          <p>Summary of items and charges</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="total-line"><span>Items:</span><strong id="sCnt">0</strong></div>
      <div class="total-line"><span>Sub-Total:</span><strong id="sSub">Rs. 0</strong></div>
      <div class="total-line"><span>GST (18%):</span><strong id="sGst">Rs. 0</strong></div>
      <div class="grand-total-box"><span>Grand Total:</span><strong id="sGrand">Rs. 0</strong></div>

      <button type="submit" class="btn btn-primary w-100 btn-lg mt-3" title="Save (Ctrl+S)"><i class="bi bi-file-earmark-check"></i> Update Proforma Invoice</button>
      <a href="/proforma/{pid}" class="btn btn-outline-secondary w-100 mt-2"><i class="bi bi-x-circle"></i> Cancel</a>

      <div class="info-box">
        <i class="bi bi-info-circle-fill"></i>
        <div>
          <strong>Changes are saved when you click Update</strong>
          <p>The PDF will reflect these changes after saving.</p>
        </div>
      </div>
    </div>
  </div>
</div>
</form>

<div class="modal fade" id="addCustModal" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <div class="modal-header">
      <h5 class="modal-title">Add New Customer</h5>
      <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
    </div>
    <div class="modal-body">
      <div id="addCustError" class="alert alert-danger py-2" style="display:none"></div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">Company Name <span class="text-danger">*</span></label>
          <input id="newCompany" class="form-control" required data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Contact Person</label>
          <input id="newContact" class="form-control" data-enter-action="submitNewCustomer()"></div>
      </div>
      <div class="mb-2"><label class="form-label">Address</label>
        <textarea id="newAddress" class="form-control" rows="4" placeholder="e.g. STREET NO.1, ANAND NAGAR&#10;NEAR KIDZ NEST SCHOOL, MAKSUDAN&#10;JALANDHAR (PUNJAB), INDIA&#10;PIN CODE - 144008"></textarea></div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">Phone</label>
          <input id="newPhone" class="form-control" data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Email</label>
          <input id="newEmail" class="form-control" data-enter-action="submitNewCustomer()"></div>
      </div>
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">GSTIN</label>
          <input id="newGstin" class="form-control" data-enter-action="submitNewCustomer()"></div>
        <div class="col-md-6 mb-2"><label class="form-label">Status</label>
          <select id="newStatus" class="form-select">
            <option value="active" selected>Active</option>
            <option value="inactive">Inactive</option>
          </select></div>
      </div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-primary" onclick="submitNewCustomer()"><i class="bi bi-plus-lg"></i> Add Customer</button>
    </div>
  </div></div>
</div>

<div class="modal fade" id="addItemModal" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <div class="modal-icon-header">
      <div class="mih-left">
        <div class="mih-icon"><i class="bi bi-box-seam"></i></div>
        <div>
          <h5 class="mih-title">Add New Item</h5>
          <div class="mih-subtitle">Add a new item to your catalog. This item will be available in quotations and invoices.</div>
        </div>
      </div>
      <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
    </div>
    <div class="modal-body">
      <div id="addItemError" class="alert alert-danger py-2" style="display:none"></div>

      <div class="mb-3">
        <label class="form-label">Item Name<span class="required-mark">*</span></label>
        <div class="field-icon-wrap">
          <i class="bi bi-box field-icon"></i>
          <input id="newItemName" class="form-control" required placeholder="Enter item name" data-enter-action="submitNewItem()">
        </div>
        <div class="field-hint">e.g. Sugar Elevator Chain 66.60 MM Pitch</div>
      </div>

      <div class="mb-3 desc-field-wrap">
        <div class="field-label-row">
          <label class="form-label">Description<span class="required-mark">*</span></label>
          <div class="field-actions">
            <button type="button" class="btn btn-outline-secondary desc-bold-btn" onclick="toggleBoldSelection(this)" title="Bold the selected text">B</button>
            <button type="button" class="btn btn-outline-secondary desc-preview-btn" onclick="toggleDescPreview(this)" title="Preview formatted text"><i class="bi bi-eye"></i> Preview</button>
          </div>
        </div>
        <div class="field-icon-wrap">
          <i class="bi bi-file-earmark-text field-icon"></i>
          <textarea id="newItemDesc" class="form-control" rows="3" maxlength="500" required
            placeholder="Enter item description" oninput="updateCounter(this,'newItemDesc-counter')"></textarea>
        </div>
        <div class="field-preview-box d-none"></div>
        <div class="field-hint-row">
          <div class="field-hint">You can select text and click <strong>Bold</strong> (or use **text**) to make it bold.</div>
          <div class="field-counter" id="newItemDesc-counter">0/500</div>
        </div>
      </div>

      <div class="mb-3">
        <label class="form-label">Specification <span class="text-muted fw-normal">(Optional)</span></label>
        <div class="field-icon-wrap">
          <i class="bi bi-gear field-icon"></i>
          <textarea id="newItemSpec" class="form-control" rows="4" maxlength="500"
            placeholder="Enter technical specification, material, size, etc." oninput="updateCounter(this,'newItemSpec-counter')"></textarea>
        </div>
        <div class="field-hint-row">
          <div class="field-hint">e.g. Material: SS304, Pitch: 66.60 MM, Breaking Load: 120 kN</div>
          <div class="field-counter" id="newItemSpec-counter">0/500</div>
        </div>
      </div>

      <div class="row">
        <div class="col-md-6 mb-3">
          <label class="form-label">Unit<span class="required-mark">*</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-tag field-icon"></i>
            <input id="newItemUnit" class="form-control" list="unitOptionsPI" value="Mtr." autocomplete="off" required data-enter-action="submitNewItem()">
          </div>
          <datalist id="unitOptionsPI">{unit_datalist_options_pi}</datalist>
          <div class="field-hint">e.g. Mtr., Pcs., Kg., Set, Box</div>
        </div>
        <div class="col-md-6 mb-3">
          <label class="form-label">Rate (Rs.)<span class="required-mark">*</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-currency-rupee field-icon"></i>
            <input id="newItemRate" type="number" class="form-control" step="0.01" min="0" placeholder="Enter rate" required data-enter-action="submitNewItem()">
          </div>
          <div class="field-hint">Enter the selling price per unit (in INR)</div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <div class="modal-footer-split">
        <div class="info-tip-box">
          <i class="bi bi-info-circle-fill"></i>
          <div class="info-tip-text"><strong>Tip:</strong> Keep item details clear and accurate. These will be used in quotations and invoices.</div>
        </div>
        <div class="footer-actions">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" onclick="submitNewItem()"><i class="bi bi-plus-lg"></i> Add Item</button>
        </div>
      </div>
    </div>
  </div></div>
</div>


<script>
{js}
</script>'''
        return self._layout(f'Edit {p["pi_no"]}', f'Edit Proforma Invoice - {p["pi_no"]}', content, 'proforma')


    # ── PAGE: PROFORMA INVOICE DETAIL ────────────────────────
    def pg_pidetail(self, pid):
        db = get_db()
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()
        c = None
        if p['customer_id']:
            c = db.execute("SELECT * FROM customers WHERE id=?",
                           (p['customer_id'],)).fetchone()
        items = db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? "
            "ORDER BY sort_order", (pid,)).fetchall()
        db.close()

        subtotal = sum((it['quantity'] or 0) * (it['rate'] or 0) for it in items)
        gst = subtotal * 0.18
        grand = subtotal + gst

        item_rows = ''
        for i, it in enumerate(items, 1):
            amt = (it['quantity'] or 0) * (it['rate'] or 0)
            net = amt * (1 + (it['gst_percent'] or 18) / 100)
            spec = esc(it['specification'] or '').replace('&#10;', '<br>')
            item_rows += (f'<tr><td class="text-center"><strong>{i}</strong></td>'
                          f'<td><strong>{esc(it["item_name"])}</strong></td>'
                          f'<td style="font-size:11px">{esc_nl(it["description"] or "")}'
                          f'<br><br><b>Specification:</b><br>{spec}</td>'
                          f'<td class="text-center">{it["quantity"]}</td>'
                          f'<td class="text-center">{esc(it["unit"])}</td>'
                          f'<td class="text-end">{fmt_rupee(it["rate"])}</td>'
                          f'<td class="text-end">{fmt_rupee(amt)}</td>'
                          f'<td class="text-center">{it["gst_percent"]:.0f}%</td>'
                          f'<td class="text-end"><strong>{fmt_rupee(net)}</strong></td></tr>')
        if not items:
            item_rows = ('<tr><td colspan="9" class="text-center text-muted py-4">'
                         'No items in this proforma invoice.</td></tr>')

        cName = esc(c['contact_person']) if c else ''
        cCompany = esc(c['company_name']) if c else ''
        cAddr = esc_nl(c['address'] or '') if c else ''
        cGstin = esc(c['gstin'] or '') if c else ''

        status_opts = ''
        for s in ['Draft', 'Sent', 'Confirmed', 'Cancelled']:
            sel = ' selected' if p['status'] == s else ''
            status_opts += f'<option value="{s}"{sel}>{s}</option>'

        content = f'''
<div class="row g-3">
  <div class="col-lg-8">
    <div class="form-section" style="padding:32px;border:1px solid #CBD5E1">

      <div class="text-center mb-3">
        <h3 style="color:#DC2626;font-weight:800;letter-spacing:2px">AYPEE  INDUSTRIES</h3>
        <div class="d-flex justify-content-between" style="font-size:12px">
          <span><u>Subject to Jalandhar Jurisdiction</u></span>
          <span>GSTIN: 03AAHFA4106G1Z4</span>
        </div>
      </div>

      <table class="table table-bordered mb-2" style="font-size:11px">
        <tr>
          <td style="width:35%"><b>Correspondence Address:</b><br>
            STREET NO.1, ANAND NAGAR<br>NEAR KIDZ NEST SCHOOL,<br>
            MAKSUDAN, JALANDHAR (PUNJAB), INDIA<br>PIN CODE - 144008</td>
          <td style="width:27%;text-align:center;vertical-align:middle">
            <b style="color:#DC2626;font-size:14px">CHAINS</b><br>
            <i>MFG. OF ALL KINDS OF S.G. IRON CHAINS,<br>FABRICATED CANE CARRIER,<br>
            RBC &amp; RAKE CARRIER CHAINS,</i></td>
          <td style="width:38%;text-align:right;vertical-align:middle">
            <b>(Off.): 0181-2672260</b><br><br><b>(Mob): 9357255758, 9041881996</b></td>
        </tr>
      </table>

      <div class="text-center" style="font-size:10px;font-weight:700;border-top:1px solid #333;border-bottom:1px solid #333;padding:4px 0">
        MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, RBC &amp; RAKE CARRIER CHAINS, SLAT &amp; SPARE PARTS
      </div>

      <div class="d-flex justify-content-between mt-2" style="font-size:11px;border-bottom:1px solid #999;padding-bottom:4px">
        <span>E-mail: <u style="color:#0000CC">aypeeinds@yahoo.com</u></span>
        <b>Date: {esc(p["date"])}</b>
      </div>

      <div style="margin-top:16px"><b>To</b><br>
        <b>{cName}</b><br><b>{cCompany}</b><br>{cAddr}<br><small>{cGstin}</small>
      </div>

      <div class="d-flex justify-content-between align-items-center my-3">
        <b>Order No: {esc(p["order_no"] or "-")}</b>
        <b style="text-decoration:underline">PROFORMA INVOICE</b>
      </div>

      <table class="table table-bordered mt-3" style="font-size:12px">
        <thead style="background:#E2E8F0">
          <tr>
            <th style="width:5%">Sr.</th><th style="width:14%">Item</th>
            <th style="width:25%">Description &amp; Specification</th>
            <th style="width:7%">Qty.</th><th style="width:6%">Unit</th>
            <th style="width:9%">Rate</th><th style="width:10%">Amount</th>
            <th style="width:6%">GST%</th><th style="width:12%">Net Total</th>
          </tr>
        </thead>
        <tbody>{item_rows}</tbody>
      </table>

      <div class="text-end" style="font-size:13px">
        <div><b>Sub-Total: Rs. {fmt_rupee(subtotal)}</b></div>
        <div><b>GST (18%): Rs. {fmt_rupee(gst)}</b></div>
        <div style="font-size:17px;color:#DC2626;border-top:2px solid #333;padding-top:4px;margin-top:4px">
          <b>Grand Total: Rs. {fmt_rupee(grand)}</b></div>
      </div>

      <div style="margin-top:24px">
        <b>Thanks &amp; Regards</b><br><br>
        <b>For Aypee Industries</b><br><br><br>
        <b>Raman Budhia (Partner)</b><br>
        <b>+91-9357255758</b>
      </div>
    </div>
  </div>

  <div class="col-lg-4">
    <div class="form-section mb-3">
      <h5><i class="bi bi-file-text"></i> Details</h5>
      <table class="table table-sm mb-0">
        <tr><td class="text-muted">No</td><td class="text-end"><b>{esc(p["pi_no"])}</b></td></tr>
        <tr><td class="text-muted">Order No</td><td class="text-end">{esc(p["order_no"] or "-")}</td></tr>
        <tr><td class="text-muted">Date</td><td class="text-end">{esc(p["date"])}</td></tr>
        <tr><td class="text-muted">Status</td>
          <td class="text-end"><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td></tr>
        <tr><td class="text-muted">Customer</td><td class="text-end">{cCompany or "-"}</td></tr>
        <tr><td class="text-muted">Created</td><td class="text-end">{esc(p["created_at"] or "-")}</td></tr>
      </table>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-calculator"></i> Financial Summary</h5>
      <div class="summary-box">
        <div class="total-line"><span>Sub-Total</span><b>Rs. {fmt_rupee(subtotal)}</b></div>
        <div class="total-line"><span>GST (18%)</span><b>Rs. {fmt_rupee(gst)}</b></div>
        <div class="total-line grand-total"><span>Grand Total</span><b>Rs. {fmt_rupee(grand)}</b></div>
      </div>
    </div>

    <div class="form-section">
      <h5><i class="bi bi-gear"></i> Actions</h5>
      <a href="/proforma/{p["id"]}/pdf" class="btn btn-danger w-100 mb-2">
        <i class="bi bi-file-earmark-pdf"></i> Download PDF</a>
      <a href="/proforma/{p["id"]}/edit" class="btn btn-primary w-100 mb-2">
        <i class="bi bi-pencil"></i> Edit Proforma Invoice</a>
      <form method="POST" action="/proforma/{p["id"]}/duplicate" class="mb-2">
        <button class="btn btn-outline-success w-100" title="Create a new proforma invoice with the same customer and items">
          <i class="bi bi-files"></i> Repeat This Proforma Invoice</button>
      </form>
      <form method="POST" action="/proforma/{p["id"]}/status" class="mb-2">
        <label class="form-label">Change Status</label>
        <select name="status" class="form-select form-select-sm" onchange="this.form.submit()">{status_opts}</select>
      </form>
      <a href="/proforma" class="btn btn-outline-primary w-100 mb-2">
        <i class="bi bi-arrow-left"></i> Back to List</a>
      <form method="POST" action="/proforma/{p["id"]}/delete"
            onsubmit="return confirm('Delete this proforma invoice permanently?')">
        <button class="btn btn-outline-danger w-100"><i class="bi bi-trash"></i> Delete</button>
      </form>
    </div>
  </div>
</div>'''
        return self._layout(p['pi_no'], p['pi_no'], content, 'proforma')
