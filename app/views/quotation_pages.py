import json
import math
import random
import sqlite3
import time
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import pdf_response
from database import get_db
from database import gen_quotation_no
from app.generators.pdf_generator import build_pdf
from datetime import datetime


class QuotationSaveError(Exception):
    """Raised when a submitted quotation can't be saved because one or
    more line items had unparseable data (bad qty/rate). Carries a list
    of human-readable per-row issues so the caller can show the user
    exactly what to fix instead of silently dropping rows."""
    def __init__(self, issues):
        self.issues = issues
        super().__init__('; '.join(issues))


class QuotationNumberConflictError(Exception):
    """Raised when a unique quotation_no couldn't be generated after
    repeated collisions with concurrent requests."""


class QuotationPagesMixin:
    QUOTATION_STATUSES = ['Draft', 'Sent', 'Confirmed', 'Cancelled']
    QUOTATIONS_PAGE_SIZE = 10
    QNO_MAX_ATTEMPTS = 5

    def _insert_quotation_row(self, db, qno_fn, values_fn, max_attempts=None):
        """Insert a new `quotations` row, retrying if the generated
        quotation_no collides with one another request just inserted.

        qno_fn: () -> str, generates a candidate quotation_no.
        values_fn: (qno) -> full parameter tuple for the INSERT, in
            (quotation_no,date,customer_id,subject,terms,status,notes,
            created_by) order.

        A short randomized backoff runs between attempts (instead of
        retrying in a tight loop) so a second colliding request is
        unlikely to still be mid-transaction by the time we retry.
        Returns (new_row_id, quotation_no). Raises
        QuotationNumberConflictError — never lets the underlying
        sqlite3.IntegrityError bubble up — if every attempt collides.
        """
        max_attempts = max_attempts or self.QNO_MAX_ATTEMPTS
        last_err = None
        for attempt in range(max_attempts):
            qno = qno_fn()
            try:
                cur = db.execute(
                    "INSERT INTO quotations "
                    "(quotation_no,date,customer_id,subject,terms,status,notes,created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    values_fn(qno))
                return cur.lastrowid, qno
            except sqlite3.IntegrityError as e:
                last_err = e
                if attempt < max_attempts - 1:
                    time.sleep(random.uniform(0.02, 0.08) * (attempt + 1))
        raise QuotationNumberConflictError(
            f'Could not generate a unique quotation number after '
            f'{max_attempts} attempts') from last_err

    def _render_save_error(self, message, back_url):
        """Simple error page for save failures (invalid item data, or a
        quotation_no collision that survived every retry). Used instead
        of redirecting to a fake "success" page so problems are always
        visible to the user rather than silently swallowed."""
        content = f'''
<div class="form-section" style="max-width:640px;margin:48px auto;padding:32px">
  <h5 style="color:#DC2626"><i class="bi bi-exclamation-triangle"></i> Could not save quotation</h5>
  <p style="margin-top:16px">{message}</p>
  <a href="{back_url}" class="btn btn-primary mt-2"><i class="bi bi-arrow-left"></i> Go back and try again</a>
</div>'''
        return self._layout('Error', 'Could Not Save', content, 'quotations')

    def handle_quotation_create(self, form, user_id):
        db = get_db()
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        # A brand-new quotation is normally a Draft, but the form lets
        # the status be set up front (e.g. straight to "Sent") — only
        # trust it if it's one of the recognised statuses.
        status = form.get('status', 'Draft')
        if status not in self.QUOTATION_STATUSES:
            status = 'Draft'

        try:
            qid, _ = self._insert_quotation_row(
                db, lambda: gen_quotation_no(db),
                lambda qno: (qno, date, customer_id, form.get('subject', ''),
                             form.get('terms', ''), status, form.get('notes', ''), user_id))
        except QuotationNumberConflictError:
            db.rollback()
            db.close()
            return self._render_save_error(
                'Another quotation was being created at the exact same '
                'moment, so a unique quotation number could not be '
                'generated. Nothing was saved &mdash; please try '
                'submitting again.',
                '/quotations/new')

        try:
            self._save_quotation_items(db, qid, form)
        except QuotationSaveError as e:
            db.rollback()
            db.close()
            issues_html = '<br>'.join(esc(issue) for issue in e.issues)
            return self._render_save_error(
                'The quotation was not saved because some items had '
                f'invalid data:<br>{issues_html}',
                '/quotations/new')

        db.commit()
        db.close()
        return self.send(*redirect(f'/quotations/{qid}'))


    def _save_quotation_items(self, db, qid, form):
        """Insert quotation_items rows from submitted form data.

        Deliberately does NOT re-look up items in the `items` catalog
        table by id. Each row's full snapshot (name, description,
        spec, unit, rate) is submitted directly from the browser
        (hidden fields kept in sync with the in-page item list), so
        this works even when item_id is blank/NULL or points at a
        catalog item that's since been deleted or edited — which
        happens whenever a quotation is duplicated/edited later.
        Re-deriving those fields from a live `items` lookup instead
        would silently drop any such row (lookup finds nothing ->
        item skipped), which is what previously caused items to
        vanish after Update/Repeat."""
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

            rows.append((qid, iid_val, name, desc, spec, qty_val, unit,
                         rate_val, 18, i))

        # Fail the whole save rather than silently dropping the bad
        # row(s) — the caller rolls back so no items (good or bad) end
        # up half-saved, and shows these messages to the user.
        if issues:
            raise QuotationSaveError(issues)

        for row in rows:
            db.execute(
                "INSERT INTO quotation_items "
                "(quotation_id,item_id,item_name,description,specification,"
                "quantity,unit,rate,gst_percent,sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                row)

    @staticmethod
    def _parse_numeric_field(raw):
        """Parse a user-submitted numeric string for qty/rate.

        Tolerates thousands separators and surrounding whitespace (e.g.
        "1,000", " 250.50 ") since that's the most common way a
        legitimate quantity/rate ends up looking "invalid" to float().
        Returns None if the value still can't be parsed as a number,
        so the caller can report it instead of silently discarding it.
        """
        if raw is None:
            return None
        cleaned = raw.strip().replace(',', '')
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None

    def handle_quotation_edit(self, qid, form, user_id):
        db = get_db()
        # Check if quotation exists
        q = db.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        if not q:
            db.close()
            return self.send(*redirect('/quotations'))

        # Update quotation
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        status = form.get('status', q['status'])
        if status not in self.QUOTATION_STATUSES:
            status = q['status']

        db.execute(
            "UPDATE quotations SET date=?, customer_id=?, subject=?, terms=?, "
            "status=?, notes=? WHERE id=?",
            (date, customer_id, form.get('subject', ''), form.get('terms', ''),
             status, form.get('notes', ''), qid))

        # Delete existing items
        db.execute("DELETE FROM quotation_items WHERE quotation_id=?", (qid,))

        # Add new items
        try:
            self._save_quotation_items(db, qid, form)
        except QuotationSaveError as e:
            # Nothing has been committed yet, so rolling back undoes
            # both the UPDATE above and the DELETE — the quotation is
            # left exactly as it was before this request.
            db.rollback()
            db.close()
            issues_html = '<br>'.join(esc(issue) for issue in e.issues)
            return self._render_save_error(
                'The quotation was not updated because some items had '
                f'invalid data:<br>{issues_html}',
                f'/quotations/{qid}/edit')

        db.commit()
        db.close()
        return self.send(*redirect(f'/quotations/{qid}'))

    def handle_quotation_duplicate(self, qid, user_id):
        """Create a brand-new quotation that's a copy of an existing one —
        same customer, subject, terms, notes and line items (name,
        description, spec, unit, rate, GST) — but with a fresh
        auto-generated quotation number, today's date, and status reset
        to Draft. Sent to the edit page afterwards so quantities, rates
        or the customer can be tweaked before it's issued."""
        db = get_db()
        src = db.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        if not src:
            db.close()
            return self.send(*redirect('/quotations'))
        src_items = db.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=? ORDER BY sort_order",
            (qid,)).fetchall()

        date = datetime.now().strftime('%d - %m - %Y')
        try:
            new_id, _ = self._insert_quotation_row(
                db, lambda: gen_quotation_no(db),
                lambda qno: (qno, date, src['customer_id'], src['subject'],
                             src['terms'], 'Draft', src['notes'], user_id))
        except QuotationNumberConflictError:
            db.rollback()
            db.close()
            return self._render_save_error(
                'This quotation could not be duplicated because another '
                'quotation was being created at the exact same moment, '
                'so a unique quotation number could not be generated. '
                'Please try again.',
                f'/quotations/{qid}')

        for it in src_items:
            db.execute(
                "INSERT INTO quotation_items "
                "(quotation_id,item_id,item_name,description,specification,"
                "quantity,unit,rate,gst_percent,sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_id, it['item_id'], it['item_name'], it['description'],
                 it['specification'], it['quantity'], it['unit'],
                 it['rate'], it['gst_percent'], it['sort_order']))
        db.commit()
        db.close()
        return self.send(*redirect(f'/quotations/{new_id}/edit'))


    def _gen_pdf(self, qid):
        db = get_db()
        qrow = db.execute("SELECT * FROM quotations WHERE id=?",
                          (qid,)).fetchone()
        if not qrow:
            db.close()
            return pdf_response(b'%PDF-1.4\n% Not found', 'not-found.pdf')
        q = dict(qrow)
        c = None
        if q.get('customer_id'):
            crow = db.execute("SELECT * FROM customers WHERE id=?",
                              (q['customer_id'],)).fetchone()
            c = dict(crow) if crow else None
        items = [dict(r) for r in db.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=? "
            "ORDER BY sort_order", (qid,)).fetchall()]
        db.close()
        pdf = build_pdf(q, c, items)
        return pdf_response(pdf, f"{q['quotation_no']}.pdf")


    # ── PAGE: QUOTATIONS LIST ────────────────────────────────
    def _quotations_rows_html(self, rows_data, start_index=1):
        """Render <tr> rows for a set of quotation records (already fetched).
        `start_index` is the running row number of the first row on this
        page (i.e. the offset + 1), so numbering stays continuous across
        pages instead of restarting at 1 every time."""
        rows = ''
        for i, q in enumerate(rows_data):
            n = start_index + i
            customer_label = q['company_name'] or q['contact_person'] or '-'
            rows += (f'<tr>'
                     f'<td><input type="checkbox" class="form-check-input q-row-check" value="{q["id"]}"></td>'
                     f'<td class="text-muted">{n}</td>'
                     f'<td><strong>{esc(q["quotation_no"])}</strong></td>'
                     f'<td>{esc(q["date"])}</td>'
                     f'<td>{esc(customer_label)}</td>'
                     f'<td>{esc(q["subject"] or "-")}</td>'
                     f'<td><span class="badge badge-{esc(q["status"].lower())}">{esc(q["status"])}</span></td>'
                     f'<td class="text-end">'
                     f'<a href="/quotations/{q["id"]}" class="row-action-btn me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/quotations/{q["id"]}/pdf" class="row-action-btn me-1 ra-pdf" title="Download PDF"><i class="bi bi-file-earmark-pdf"></i></a>'
                     f'<a href="/quotations/{q["id"]}/edit" class="row-action-btn me-1 ra-edit" title="Edit"><i class="bi bi-pencil"></i></a>'
                     f'<form method="POST" action="/quotations/{q["id"]}/delete" class="d-inline" '
                     f'onsubmit="return confirm(\'Delete quotation {esc(q["quotation_no"])} permanently?\')">'
                     f'<button type="submit" class="row-action-btn ra-delete" title="Delete"><i class="bi bi-trash"></i></button></form>'
                     f'</td></tr>')
        return rows

    def _quotations_empty_state(self, has_any, q, status):
        """Illustration + message for zero results, whether the table is
        truly empty or just has no matches for the current search/filter."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-file-earmark-text"></i></div>'
                    '<h6>No quotations yet</h6>'
                    '<p class="text-muted">Once you create a quotation, it will show up here.</p>'
                    '<a href="/quotations/new" class="btn btn-primary btn-sm">'
                    '<i class="bi bi-plus-circle"></i> Create your first quotation</a>'
                    '</div>')
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching quotations</h6>'
                '<p class="text-muted">Try a different search term'
                + (f' or clear the "{esc(status)}" filter' if status else '')
                + '.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="qClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _quotations_pagination_html(self, page, total_pages, q, status):
        html = '<nav aria-label="Quotations pagination"><ul class="pagination pagination-sm q-pagination mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="qGoToPage(event,{p})">{label}</a></li>')

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

    def _quotations_chips_html(self, status_counts, all_count, status):
        chips = [('', 'All', all_count)] + [(s, s, status_counts.get(s, 0)) for s in self.QUOTATION_STATUSES]
        html = ''
        for val, label, count in chips:
            active = ' active' if val == status else ''
            html += (f'<button type="button" class="q-chip{active}" data-status="{esc(val)}" '
                     f'onclick="qSetStatus(\'{esc(val)}\')">{esc(label)} '
                     f'<span class="q-chip-count">{count}</span></button>')
        return html

    def _quotations_stat_cards_html(self, status_counts, all_count):
        """Five summary cards (Total / Draft / Sent / Confirmed / Cancelled)
        shown above the quotations table."""
        cards = [
            ('Total Quotations', all_count, 'bi-file-earmark-text', '#3B82F6', '#EFF6FF'),
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

    def _quotations_fragment(self, q='', status='', page='1', page_size=None):
        """Fetch, filter, and paginate quotations; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/filter/pagination without a full reload."""
        q = (q or '').strip()
        status = status or ''
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        if status not in self.QUOTATION_STATUSES:
            status = ''

        try:
            page_size = int(page_size) if page_size else self.QUOTATIONS_PAGE_SIZE
        except (TypeError, ValueError):
            page_size = self.QUOTATIONS_PAGE_SIZE
        if page_size not in (10, 25, 50, 100):
            page_size = self.QUOTATIONS_PAGE_SIZE

        db = get_db()

        search_where, search_params = [], []
        if q:
            like = f"%{q}%"
            search_where.append(
                "(q.quotation_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR q.date LIKE ?)")
            search_params.extend([like, like, like, like])
        search_sql = (" WHERE " + " AND ".join(search_where)) if search_where else ""

        # Whether the table has ANY data at all (ignoring filters) — decides
        # which empty-state message to show.
        has_any = db.execute("SELECT COUNT(*) c FROM quotations").fetchone()['c'] > 0

        # Status counts respect the search term so chip counts stay accurate
        # while typing, but not the status filter itself.
        all_count = db.execute(
            f"SELECT COUNT(*) c FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id{search_sql}",
            search_params).fetchone()['c']
        status_counts = {}
        for s in self.QUOTATION_STATUSES:
            join = " AND" if search_where else " WHERE"
            r = db.execute(
                f"SELECT COUNT(*) c FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id{search_sql}{join} q.status=?",
                search_params + [s]).fetchone()
            status_counts[s] = r['c']

        where = list(search_where)
        params = list(search_params)
        if status:
            where.append("q.status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = db.execute(
            f"SELECT COUNT(*) c FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id{where_sql}",
            params).fetchone()['c']
        total_pages = max(1, math.ceil(total / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        rows_data = db.execute(
            f"SELECT q.*,c.company_name,c.contact_person FROM quotations q "
            f"LEFT JOIN customers c ON q.customer_id=c.id{where_sql} "
            f"ORDER BY q.created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]).fetchall()
        db.close()

        if rows_data:
            rows_html = self._quotations_rows_html(rows_data, start_index=offset + 1)
        else:
            rows_html = f'<tr><td colspan="8" class="p-0">{self._quotations_empty_state(has_any, q, status)}</td></tr>'

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + page_size, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total} quotations'
                       if total else 'No quotations found')

        return {
            'rows_html': rows_html,
            'pagination_html': self._quotations_pagination_html(page, total_pages, q, status),
            'chips_html': self._quotations_chips_html(status_counts, all_count, status),
            'stat_cards_html': self._quotations_stat_cards_html(status_counts, all_count),
            'count_label': count_label,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        }

    def pg_quotations(self, q='', status='', page='1', page_size=None):
        frag = self._quotations_fragment(q=q, status=status, page=page, page_size=page_size)
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
#quotationsTbody {{ transition:opacity .1s; }}
#quotationsTbody.q-loading {{ opacity:.4; }}

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
    <div class="q-page-head-icon"><i class="bi bi-file-earmark-text"></i></div>
    <div>
      <h1>All Quotations</h1>
      <p>View and manage all customer quotations</p>
    </div>
  </div>
  <a href="/quotations/new" class="btn btn-primary"><i class="bi bi-plus-circle"></i> New Quotation</a>
</div>

<div class="row g-3 mb-3" id="qStatCards">{frag["stat_cards_html"]}</div>

<div class="table-card">
  <div class="p-3 border-bottom">
    <div class="q-toolbar">
      <div class="input-group input-group-sm q-search-wrap">
        <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
        <input type="text" id="qSearchInput" class="form-control border-start-0" placeholder="Search by quotation no., customer, subject or date..." value="{esc(q)}">
      </div>
      <button type="button" class="btn btn-outline-secondary btn-sm q-date-btn" title="Filter by date">
        <i class="bi bi-calendar3"></i> Select Date
        <input type="date" id="qDateInput" aria-label="Select date">
      </button>
      <div class="q-chips" id="qChips">{frag["chips_html"]}</div>
      <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" id="qExportBtn"><i class="bi bi-download"></i> Export</button>
    </div>
  </div>
  <div class="table-responsive-wrapper">
  <table class="table mb-0">
    <thead>
      <tr>
        <th style="width:36px"><input type="checkbox" class="form-check-input" id="qSelectAll"></th>
        <th style="width:40px">#</th>
        <th>Quotation No.</th>
        <th class="q-th-sort">Date <i class="bi bi-arrow-down-up"></i></th>
        <th class="q-th-sort">Customer <i class="bi bi-arrow-down-up"></i></th>
        <th class="q-th-sort">Subject <i class="bi bi-arrow-down-up"></i></th>
        <th>Status</th>
        <th class="text-end">Actions</th>
      </tr>
    </thead>
    <tbody id="quotationsTbody">{frag["rows_html"]}</tbody>
  </table>
  </div>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="qCountLabel">{frag["count_label"]}</span>
    <div class="d-flex align-items-center gap-2">
      <div id="qPagination">{frag["pagination_html"]}</div>
      <select class="q-pagesize-select" id="qPageSize">{size_opts}</select>
    </div>
  </div>
</div>

<div class="q-tips-card">
  <div class="q-tips-icon"><i class="bi bi-info-lg"></i></div>
  <div>
    <h6>Quick Tips</h6>
    <ul>
      <li>Create a new quotation to send to your customer.</li>
      <li>Use filters to quickly find quotations by status or date.</li>
      <li>You can view, edit or duplicate a quotation from the actions menu.</li>
    </ul>
  </div>
</div>

<script>
(function() {{
  var state = {{
    q: {json.dumps(q)}, status: {json.dumps(status)},
    page: {frag["page"]}, pageSize: {frag["page_size"]}
  }};
  var debounceTimer = null;

  function refresh() {{
    var tbody = document.getElementById('quotationsTbody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    params.set('page', state.page);
    params.set('page_size', state.pageSize);
    fetch('/api/quotations?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('qPagination').innerHTML = data.pagination_html;
        document.getElementById('qChips').innerHTML = data.chips_html;
        document.getElementById('qCountLabel').innerHTML = data.count_label;
        document.getElementById('qStatCards').innerHTML = data.stat_cards_html;
        document.getElementById('qSelectAll').checked = false;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.status) url.searchParams.set('status', state.status); else url.searchParams.delete('status');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        if (state.pageSize !== 10) url.searchParams.set('page_size', state.pageSize); else url.searchParams.delete('page_size');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.qGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.qSetStatus = function(s) {{
    state.status = s;
    state.page = 1;
    refresh();
  }};

  window.qClearFilters = function() {{
    state.q = '';
    state.status = '';
    state.page = 1;
    document.getElementById('qSearchInput').value = '';
    document.getElementById('qDateInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('qSearchInput');
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

  document.getElementById('qPageSize').addEventListener('change', function(e) {{
    state.pageSize = parseInt(e.target.value, 10) || 10;
    state.page = 1;
    refresh();
  }});

  document.getElementById('qDateInput').addEventListener('change', function(e) {{
    if (!e.target.value) return;
    var d = new Date(e.target.value + 'T00:00:00');
    var day = String(d.getDate()).padStart(2, '0');
    var month = d.toLocaleString('en-US', {{ month: 'short' }});
    var formatted = day + ' ' + month + ', ' + d.getFullYear();
    searchInput.value = formatted;
    state.q = formatted;
    state.page = 1;
    refresh();
  }});

  document.getElementById('qSelectAll').addEventListener('change', function(e) {{
    document.querySelectorAll('.q-row-check').forEach(function(cb) {{ cb.checked = e.target.checked; }});
  }});

  document.getElementById('qExportBtn').addEventListener('click', function() {{
    var rows = [['Quotation No.', 'Date', 'Customer', 'Subject', 'Status']];
    document.querySelectorAll('#quotationsTbody tr').forEach(function(tr) {{
      var cells = tr.querySelectorAll('td');
      if (cells.length < 7) return;
      rows.push([
        cells[2].innerText.trim(), cells[3].innerText.trim(),
        cells[4].innerText.trim(), cells[5].innerText.trim(), cells[6].innerText.trim()
      ]);
    }});
    var csv = rows.map(function(r) {{
      return r.map(function(v) {{ return '"' + String(v).replace(/"/g, '""') + '"'; }}).join(',');
    }}).join('\\r\\n');
    var blob = new Blob([csv], {{ type: 'text/csv;charset=utf-8;' }});
    var link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'quotations.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }});
}})();
</script>'''
        return self._layout('Quotations', 'All Quotations', content, 'quotations')


    # ── PAGE: NEW QUOTATION ──────────────────────────────────
    def pg_qnew(self):
        db = get_db()
        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        item_cat_rows = db.execute(
            "SELECT ic.item_id AS item_id, c.id AS cat_id, c.name AS cat_name "
            "FROM item_categories ic JOIN categories c ON ic.category_id = c.id").fetchall()
        qno = gen_quotation_no(db)
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

        status_opts = ''
        for s in self.QUOTATION_STATUSES:
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
let quotationItems = [];
let itemCounter = 0;

function escapeHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function categoryLabel(item) {{
  return (item.categories && item.categories.length) ? item.categories[0].name : 'General';
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
// combine (AND), same as the old customer+category item filter did.
function refreshItemOptions() {{
  const catId = document.getElementById('catSel').value;
  const q = document.getElementById('itemSearch').value.trim().toLowerCase();
  const sel = document.getElementById('itemSelect');
  const prev = sel.value;
  sel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Select an item...';
  sel.appendChild(placeholder);
  allItems.filter(function(it) {{
    if (catId && !(it.categories || []).some(function(c) {{ return String(c.id) === catId; }})) return false;
    if (q && it.name.toLowerCase().indexOf(q) === -1) return false;
    return true;
  }}).forEach(function(it) {{
    const opt = document.createElement('option');
    opt.value = it.id;
    opt.dataset.name = it.name;
    opt.dataset.desc = it.desc;
    opt.dataset.spec = it.spec;
    opt.dataset.unit = it.unit;
    opt.dataset.rate = it.rate;
    opt.dataset.cat = categoryLabel(it);
    opt.textContent = it.name + ' - Rs. ' + Number(it.rate).toLocaleString('en-IN') + '/' + it.unit;
    sel.appendChild(opt);
  }});
  if ([...sel.options].some(function(o) {{ return o.value === prev; }})) sel.value = prev;
}}

function addItemRow() {{
  const sel = document.getElementById('itemSelect');
  const qtyInput = document.getElementById('qtyInput');
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  itemCounter++;
  quotationItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: parseFloat(opt.dataset.rate),
    cat: opt.dataset.cat,
    qty: qty
  }});
  sel.value = '';
  qtyInput.value = '1';
  renderItems();
}}

function removeItem(id) {{
  quotationItems = quotationItems.filter(function(i) {{ return i.id !== id; }});
  renderItems();
}}

function renderItems() {{
  const body = document.getElementById('itemsBody');
  document.getElementById('itemsCount').textContent = quotationItems.length;
  if (quotationItems.length === 0) {{
    body.innerHTML = '<tr class="empty-row"><td colspan="7">' +
      '<i class="bi bi-box"></i>' +
      '<div class="empty-title">No items added yet.</div>' +
      '<div class="empty-sub">Search and add items from your catalog to create this quotation.</div>' +
      '</td></tr>';
    updSum();
    return;
  }}
  let rows = '';
  quotationItems.forEach(function(item, idx) {{
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
      '<td>' + escapeHtml(item.name) + '</td>' +
      '<td class="text-muted">' + escapeHtml(item.cat) + '</td>' +
      '<td>' + item.qty.toLocaleString('en-IN', loc) + ' ' + escapeHtml(item.unit) + '</td>' +
      '<td>Rs. ' + item.rate.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</td>' +
      '<td><strong>Rs. ' + amt.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</strong></td>' +
      '<td><button type="button" class="btn btn-sm btn-link text-danger p-0" onclick="removeItem(' + item.id + ')" title="Remove"><i class="bi bi-trash"></i></button></td>' +
      '</tr>';
  }});
  body.innerHTML = rows;
  updSum();
}}

function updSum() {{
  let subtotal = 0;
  quotationItems.forEach(function(item) {{ subtotal += item.qty * item.rate; }});
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  const loc = {{maximumFractionDigits: 0}};
  document.getElementById('sCnt').textContent = quotationItems.length;
  document.getElementById('sSub').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN', loc);
  document.getElementById('sGst').textContent = 'Rs. ' + gst.toLocaleString('en-IN', loc);
  document.getElementById('sGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN', loc);
  checkValid();
}}

function checkValid() {{
  const custOk = document.getElementById('custSel').value !== '';
  const subjOk = document.getElementById('subjInput').value.trim() !== '';
  const itemsOk = quotationItems.length > 0;
  document.getElementById('validationHint').style.display =
    (custOk && subjOk && itemsOk) ? 'none' : 'block';
}}

function submitNewCustomer() {{
  const contact = document.getElementById('newContact').value.trim();
  const company = document.getElementById('newCompany').value.trim();
  const err = document.getElementById('addCustError');
  err.style.display = 'none';
  if (!contact || !company) {{
    err.textContent = 'Contact person and company name are required.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    contact_person: contact, company_name: company,
    address: document.getElementById('newAddress').value,
    phone: document.getElementById('newPhone').value,
    email: document.getElementById('newEmail').value,
    gstin: document.getElementById('newGstin').value
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

function submitQuotationForm() {{
  if (quotationItems.length === 0) {{
    alert('Please add at least one item to the quotation.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{
  refreshItemOptions();
  renderItems();
  checkValid();
}});
"""

        content = f'''
<div class="page-head">
  <div class="page-head-left">
    <i class="bi bi-file-earmark-text"></i>
    <div>
      <h1>Create New Quotation</h1>
      <p>Create a new quotation for your customer</p>
    </div>
  </div>
  <a href="/quotations" class="btn btn-outline-secondary"><i class="bi bi-arrow-left"></i> Back to Quotations</a>
</div>

<form method="POST" action="/quotations/new" id="qForm" onsubmit="return submitQuotationForm()">
<div class="row g-3">
  <div class="col-lg-7">

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Quotation Details</h3>
          <p>Basic information for this quotation</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-4">
          <label class="form-label">Quotation No.</label>
          <input class="form-control" value="{esc(qno)}" readonly style="background:#F8FAFC">
          <small class="text-muted" style="font-size:11px">Auto-generated</small>
        </div>
        <div class="col-md-4">
          <label class="form-label">Date</label>
          <div class="input-icon">
            <i class="bi bi-calendar3"></i>
            <input name="date" class="form-control" value="{esc(today)}">
          </div>
        </div>
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
          <p>Select items and quantities for this quotation</p>
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
        <small>Add items from the catalog or search directly. Enter quantity for each item and add to the list.</small>
        <a href="/categories"><i class="bi bi-gear"></i> Manage categories</a>
      </div>

      <div class="add-item-box">
        <div class="box-label">Add Item</div>
        <div class="row g-2 align-items-end">
          <div class="col-md-7">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="itemSelect"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="qtyInput" min="0.01" step="0.01" value="1">
          </div>
          <div class="col-md-3">
            <button type="button" class="btn btn-primary w-100" onclick="addItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
          </div>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-list-ul"></i>
        <div class="qc-head-text">
          <h3>Items in Quotation (<span id="itemsCount">0</span>)</h3>
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
              <div class="empty-sub">Search and add items from your catalog to create this quotation.</div>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Subject &amp; Terms</h3>
          <p>Add a subject, terms and any additional notes</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-6">
          <label class="form-label">Subject <span style="color:var(--primary)">*</span></label>
          <input name="subject" class="form-control" id="subjInput" placeholder="e.g. Sugar Elevator Chain" oninput="checkValid()" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Notes (Internal)</label>
          <textarea name="notes" class="form-control" rows="2" placeholder="Optional internal notes (not visible to customer)"></textarea>
        </div>
      </div>
      <div class="mt-3">
        <label class="form-label">Terms &amp; Conditions</label>
        <textarea name="terms" class="form-control" rows="4">1. Rates are Ex-works.
2. GST 18% Extra as applicable.
3. Payment: 20% Advance and Balance against delivery.
4. Delivery: 10-12 weeks after receipt of order.</textarea>
      </div>
    </div>

  </div>

  <div class="col-lg-5">
    <div class="form-section" style="position:sticky;top:86px">
      <div class="qc-head">
        <i class="bi bi-bar-chart"></i>
        <div class="qc-head-text">
          <h3>Quotation Summary</h3>
          <p>Summary of items and charges</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="total-line"><span>Items:</span><strong id="sCnt">0</strong></div>
      <div class="total-line"><span>Sub-Total:</span><strong id="sSub">Rs. 0</strong></div>
      <div class="total-line"><span>GST (18%):</span><strong id="sGst">Rs. 0</strong></div>
      <div class="grand-total-box"><span>Grand Total:</span><strong id="sGrand">Rs. 0</strong></div>

      <button type="submit" class="btn btn-primary w-100 btn-lg mt-3" id="submitBtn"><i class="bi bi-file-earmark-check"></i> Create Quotation</button>

      <div class="info-box">
        <i class="bi bi-info-circle-fill"></i>
        <div>
          <strong>PDF will be available after creation</strong>
          <p id="validationHint">Add a customer, subject and at least one item to continue.</p>
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
      <div class="tip-row tip-done"><i class="bi bi-check-circle-fill"></i> Review terms and conditions</div>
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
      <div class="mb-2"><label class="form-label">Contact Person</label>
        <input id="newContact" class="form-control" required></div>
      <div class="mb-2"><label class="form-label">Company Name</label>
        <input id="newCompany" class="form-control" required></div>
      <div class="mb-2"><label class="form-label">Address</label>
        <textarea id="newAddress" class="form-control" rows="3"></textarea></div>
      <div class="row">
        <div class="col-6 mb-2"><label class="form-label">Phone</label>
          <input id="newPhone" class="form-control"></div>
        <div class="col-6 mb-2"><label class="form-label">Email</label>
          <input id="newEmail" class="form-control"></div>
      </div>
      <div class="mb-2"><label class="form-label">GSTIN</label>
        <input id="newGstin" class="form-control"></div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-primary" onclick="submitNewCustomer()"><i class="bi bi-plus-lg"></i> Add Customer</button>
    </div>
  </div></div>
</div>

<script>
{js}
</script>'''
        return self._layout('New Quotation', 'Create New Quotation', content, 'quotations')


    # ── PAGE: EDIT QUOTATION ─────────────────────────────────
    def pg_qedit(self, qid):
        db = get_db()
        q = db.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        if not q:
            db.close()
            return self.pg_404()

        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        item_cat_rows = db.execute(
            "SELECT ic.item_id AS item_id, c.id AS cat_id, c.name AS cat_name "
            "FROM item_categories ic JOIN categories c ON ic.category_id = c.id").fetchall()
        q_items = db.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=? ORDER BY sort_order",
            (qid,)).fetchall()
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
            sel = ' selected' if q['customer_id'] == c['id'] else ''
            cust_opts += (f'<option value="{c["id"]}"{sel} '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        cat_opts = '<option value="">All Categories</option>'
        for cat in categories:
            cat_opts += f'<option value="{cat["id"]}">{esc(cat["name"])}</option>'

        status_opts = ''
        for s in self.QUOTATION_STATUSES:
            sel = ' selected' if s == q['status'] else ''
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
            existing quotation items from ever being rendered on edit."""
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
            quotation with one null rate/qty/item_id silently shows
            no items at all on the edit page."""
            return 'null' if val is None else val

        existing_items_js = '['
        for idx, it in enumerate(q_items):
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
let quotationItems = {existing_items_js};
let itemCounter = {len(q_items)};

function escapeHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function categoryLabel(item) {{
  return (item.categories && item.categories.length) ? item.categories[0].name : 'General';
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
function refreshItemOptions() {{
  const catId = document.getElementById('catSel').value;
  const q = document.getElementById('itemSearch').value.trim().toLowerCase();
  const sel = document.getElementById('itemSelect');
  const prev = sel.value;
  sel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Select an item...';
  sel.appendChild(placeholder);
  allItems.filter(function(it) {{
    if (catId && !(it.categories || []).some(function(c) {{ return String(c.id) === catId; }})) return false;
    if (q && it.name.toLowerCase().indexOf(q) === -1) return false;
    return true;
  }}).forEach(function(it) {{
    const opt = document.createElement('option');
    opt.value = it.id;
    opt.dataset.name = it.name;
    opt.dataset.desc = it.desc;
    opt.dataset.spec = it.spec;
    opt.dataset.unit = it.unit;
    opt.dataset.rate = it.rate;
    opt.dataset.cat = categoryLabel(it);
    opt.textContent = it.name + ' - Rs. ' + Number(it.rate).toLocaleString('en-IN') + '/' + it.unit;
    sel.appendChild(opt);
  }});
  if ([...sel.options].some(function(o) {{ return o.value === prev; }})) sel.value = prev;
}}

function addItemRow() {{
  const sel = document.getElementById('itemSelect');
  const qtyInput = document.getElementById('qtyInput');
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  const qty = parseFloat(qtyInput.value);
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  itemCounter++;
  quotationItems.push({{
    id: itemCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc || '',
    spec: opt.dataset.spec || '',
    unit: opt.dataset.unit,
    rate: parseFloat(opt.dataset.rate),
    cat: opt.dataset.cat,
    qty: qty
  }});
  sel.value = '';
  qtyInput.value = '1';
  renderItems();
}}

function removeItem(id) {{
  quotationItems = quotationItems.filter(function(i) {{ return i.id !== id; }});
  renderItems();
}}

function renderItems() {{
  const body = document.getElementById('itemsBody');
  document.getElementById('itemsCount').textContent = quotationItems.length;
  if (quotationItems.length === 0) {{
    body.innerHTML = '<tr class="empty-row"><td colspan="7">' +
      '<i class="bi bi-box"></i>' +
      '<div class="empty-title">No items added yet.</div>' +
      '<div class="empty-sub">Search and add items from your catalog to create this quotation.</div>' +
      '</td></tr>';
    updSum();
    return;
  }}
  let rows = '';
  quotationItems.forEach(function(item, idx) {{
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
      '<td>' + escapeHtml(item.name) + '</td>' +
      '<td class="text-muted">' + escapeHtml(item.cat) + '</td>' +
      '<td>' + item.qty.toLocaleString('en-IN', loc) + ' ' + escapeHtml(item.unit) + '</td>' +
      '<td>Rs. ' + item.rate.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</td>' +
      '<td><strong>Rs. ' + amt.toLocaleString('en-IN', {{maximumFractionDigits: 0}}) + '</strong></td>' +
      '<td><button type="button" class="btn btn-sm btn-link text-danger p-0" onclick="removeItem(' + item.id + ')" title="Remove"><i class="bi bi-trash"></i></button></td>' +
      '</tr>';
  }});
  body.innerHTML = rows;
  updSum();
}}

function updSum() {{
  let subtotal = 0;
  quotationItems.forEach(function(item) {{ subtotal += item.qty * item.rate; }});
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  const loc = {{maximumFractionDigits: 0}};
  document.getElementById('sCnt').textContent = quotationItems.length;
  document.getElementById('sSub').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN', loc);
  document.getElementById('sGst').textContent = 'Rs. ' + gst.toLocaleString('en-IN', loc);
  document.getElementById('sGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN', loc);
}}

function submitNewCustomer() {{
  const contact = document.getElementById('newContact').value.trim();
  const company = document.getElementById('newCompany').value.trim();
  const err = document.getElementById('addCustError');
  err.style.display = 'none';
  if (!contact || !company) {{
    err.textContent = 'Contact person and company name are required.';
    err.style.display = 'block';
    return;
  }}
  const payload = {{
    contact_person: contact, company_name: company,
    address: document.getElementById('newAddress').value,
    phone: document.getElementById('newPhone').value,
    email: document.getElementById('newEmail').value,
    gstin: document.getElementById('newGstin').value
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

function submitQuotationForm() {{
  if (quotationItems.length === 0) {{
    alert('Please add at least one item to the quotation.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{
  refreshItemOptions();
  renderItems();
  fillCust();
}});
"""

        content = f'''
<div class="page-head">
  <div class="page-head-left">
    <i class="bi bi-file-earmark-text"></i>
    <div>
      <h1>Edit Quotation</h1>
      <p>Update quotation {esc(q["quotation_no"])}</p>
    </div>
  </div>
  <a href="/quotations/{qid}" class="btn btn-outline-secondary"><i class="bi bi-arrow-left"></i> Back to Quotation</a>
</div>

<form method="POST" action="/quotations/{qid}/edit" id="qForm" onsubmit="return submitQuotationForm()">
<div class="row g-3">
  <div class="col-lg-7">

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Quotation Details</h3>
          <p>Basic information for this quotation</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-4">
          <label class="form-label">Quotation No.</label>
          <input class="form-control" value="{esc(q['quotation_no'])}" readonly style="background:#F8FAFC">
        </div>
        <div class="col-md-4">
          <label class="form-label">Date</label>
          <div class="input-icon">
            <i class="bi bi-calendar3"></i>
            <input name="date" class="form-control" value="{esc(q['date'])}" required>
          </div>
        </div>
        <div class="col-md-4">
          <label class="form-label">Status</label>
          <div class="status-select-wrap">
            <span class="status-dot status-dot-{esc(q['status'].lower())}" id="statusDot"></span>
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
        <div class="mt-2 p-3 bg-light rounded" id="custInfo" style="display:{'block' if q['customer_id'] else 'none'}">
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
          <p>Select items and quantities for this quotation</p>
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
        <small>Add items from the catalog or search directly. Enter quantity for each item and add to the list.</small>
        <a href="/categories"><i class="bi bi-gear"></i> Manage categories</a>
      </div>

      <div class="add-item-box">
        <div class="box-label">Add Item</div>
        <div class="row g-2 align-items-end">
          <div class="col-md-7">
            <label class="form-label">Item <span style="color:var(--primary)">*</span></label>
            <select class="form-select" id="itemSelect"><option value="">Select an item...</option></select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Quantity <span style="color:var(--primary)">*</span></label>
            <input type="number" class="form-control" id="qtyInput" min="0.01" step="0.01" value="1">
          </div>
          <div class="col-md-3">
            <button type="button" class="btn btn-primary w-100" onclick="addItemRow()"><i class="bi bi-plus-circle"></i> Add Item</button>
          </div>
        </div>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-list-ul"></i>
        <div class="qc-head-text">
          <h3>Items in Quotation (<span id="itemsCount">0</span>)</h3>
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
              <div class="empty-sub">Search and add items from your catalog to create this quotation.</div>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="form-section mb-3">
      <div class="qc-head">
        <i class="bi bi-file-earmark-text"></i>
        <div class="qc-head-text">
          <h3>Subject &amp; Terms</h3>
          <p>Add a subject, terms and any additional notes</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="row g-3">
        <div class="col-md-6">
          <label class="form-label">Subject <span style="color:var(--primary)">*</span></label>
          <input name="subject" class="form-control" id="subjInput" value="{esc(q['subject'] or '')}" placeholder="e.g. Sugar Elevator Chain" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Notes (Internal)</label>
          <textarea name="notes" class="form-control" rows="2" placeholder="Optional internal notes (not visible to customer)">{esc(q['notes'] or '')}</textarea>
        </div>
      </div>
      <div class="mt-3">
        <label class="form-label">Terms &amp; Conditions</label>
        <textarea name="terms" class="form-control" rows="4">{esc(q['terms'] or '')}</textarea>
      </div>
    </div>

  </div>

  <div class="col-lg-5">
    <div class="form-section" style="position:sticky;top:86px">
      <div class="qc-head">
        <i class="bi bi-bar-chart"></i>
        <div class="qc-head-text">
          <h3>Quotation Summary</h3>
          <p>Summary of items and charges</p>
        </div>
      </div>
      <hr class="qc-divider">
      <div class="total-line"><span>Items:</span><strong id="sCnt">0</strong></div>
      <div class="total-line"><span>Sub-Total:</span><strong id="sSub">Rs. 0</strong></div>
      <div class="total-line"><span>GST (18%):</span><strong id="sGst">Rs. 0</strong></div>
      <div class="grand-total-box"><span>Grand Total:</span><strong id="sGrand">Rs. 0</strong></div>

      <button type="submit" class="btn btn-primary w-100 btn-lg mt-3"><i class="bi bi-file-earmark-check"></i> Update Quotation</button>
      <a href="/quotations/{qid}" class="btn btn-outline-secondary w-100 mt-2"><i class="bi bi-x-circle"></i> Cancel</a>

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
      <div class="mb-2"><label class="form-label">Contact Person</label>
        <input id="newContact" class="form-control" required></div>
      <div class="mb-2"><label class="form-label">Company Name</label>
        <input id="newCompany" class="form-control" required></div>
      <div class="mb-2"><label class="form-label">Address</label>
        <textarea id="newAddress" class="form-control" rows="3"></textarea></div>
      <div class="row">
        <div class="col-6 mb-2"><label class="form-label">Phone</label>
          <input id="newPhone" class="form-control"></div>
        <div class="col-6 mb-2"><label class="form-label">Email</label>
          <input id="newEmail" class="form-control"></div>
      </div>
      <div class="mb-2"><label class="form-label">GSTIN</label>
        <input id="newGstin" class="form-control"></div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-primary" onclick="submitNewCustomer()"><i class="bi bi-plus-lg"></i> Add Customer</button>
    </div>
  </div></div>
</div>

<script>
{js}
</script>'''
        return self._layout(f'Edit {q["quotation_no"]}', f'Edit Quotation - {q["quotation_no"]}', content, 'quotations')


    # ── PAGE: QUOTATION DETAIL ───────────────────────────────
    def pg_qdetail(self, qid):
        db = get_db()
        q = db.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        if not q:
            db.close()
            return self.pg_404()
        c = None
        if q['customer_id']:
            c = db.execute("SELECT * FROM customers WHERE id=?",
                           (q['customer_id'],)).fetchone()
        items = db.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=? "
            "ORDER BY sort_order", (qid,)).fetchall()
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
                         'No items in this quotation.</td></tr>')

        cName = esc(c['contact_person']) if c else ''
        cCompany = esc(c['company_name']) if c else ''
        cAddr = esc_nl(c['address'] or '') if c else ''
        cGstin = esc(c['gstin'] or '') if c else ''

        status_opts = ''
        for s in ['Draft', 'Sent', 'Confirmed', 'Cancelled']:
            sel = ' selected' if q['status'] == s else ''
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
          <td style="width:35%;text-align:center;vertical-align:middle">
            <b style="color:#DC2626;font-size:16px">CHAINS</b><br>
            <i>MFG. OF ALL KINDS OF S.G. IRON CHAINS,<br>FABRICATED CANE CARRIER,<br>
            RBC &amp; RAKE CARRIER CHAINS,</i></td>
          <td style="width:30%;text-align:right;vertical-align:middle">
            <b>(Off.): 0181-2672260</b><br><br><b>(Mob): 9357255758, 9041881996</b></td>
        </tr>
      </table>

      <div class="text-center" style="font-size:10px;font-weight:700;border-top:1px solid #333;border-bottom:1px solid #333;padding:4px 0">
        MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, RBC &amp; RAKE CARRIER CHAINS, SLAT &amp; SPARE PARTS
      </div>

      <div class="d-flex justify-content-between mt-2" style="font-size:11px;border-bottom:1px solid #999;padding-bottom:4px">
        <span>E-mail: <u style="color:#0000CC">aypeeinds@yahoo.com</u></span>
        <b>Date: {esc(q["date"])}</b>
      </div>

      <div style="margin-top:16px"><b>To</b><br>
        <b>{cName}</b><br><b>{cCompany}</b><br>{cAddr}<br><small>{cGstin}</small>
      </div>

      <div class="text-center my-3">
        <b style="text-decoration:underline">Sub: Quotation for {esc(q["subject"])}</b>
      </div>

      <p>Dear Sir,</p>
      <p>We are pleased to quote you the best rates for your inquiry for {esc(q["subject"])}. Thank you for your interest in our products.</p>

      <table class="table table-bordered mt-3" style="font-size:12px">
        <thead style="background:#E2E8F0">
          <tr>
            <th style="width:5%">Sr.</th><th style="width:20%">Item</th>
            <th style="width:25%">Description &amp; Specification</th>
            <th style="width:7%">Qty.</th><th style="width:7%">Unit</th>
            <th style="width:11%">Rate</th><th style="width:10%">Amount</th>
            <th style="width:5%">GST%</th><th style="width:10%">Net Total</th>
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

      <div style="margin-top:16px">
        <b>Terms and Conditions</b>
        <div style="font-size:11px;white-space:pre-line;margin-left:16px">{esc(q["terms"])}</div>
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
        <tr><td class="text-muted">No</td><td class="text-end"><b>{esc(q["quotation_no"])}</b></td></tr>
        <tr><td class="text-muted">Date</td><td class="text-end">{esc(q["date"])}</td></tr>
        <tr><td class="text-muted">Status</td>
          <td class="text-end"><span class="badge badge-{esc(q["status"].lower())}">{esc(q["status"])}</span></td></tr>
        <tr><td class="text-muted">Customer</td><td class="text-end">{cCompany or "-"}</td></tr>
        <tr><td class="text-muted">Created</td><td class="text-end">{esc(q["created_at"] or "-")}</td></tr>
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
      <a href="/quotations/{q["id"]}/pdf" class="btn btn-danger w-100 mb-2">
        <i class="bi bi-file-earmark-pdf"></i> Download PDF</a>
      <a href="/quotations/{q["id"]}/edit" class="btn btn-primary w-100 mb-2">
        <i class="bi bi-pencil"></i> Edit Quotation</a>
      <form method="POST" action="/quotations/{q["id"]}/duplicate" class="mb-2">
        <button class="btn btn-outline-success w-100" title="Create a new quotation with the same customer and items">
          <i class="bi bi-files"></i> Repeat This Quotation</button>
      </form>
      <form method="POST" action="/quotations/{q["id"]}/status" class="mb-2">
        <label class="form-label">Change Status</label>
        <select name="status" class="form-select form-select-sm" onchange="this.form.submit()">{status_opts}</select>
      </form>
      <a href="/quotations" class="btn btn-outline-primary w-100 mb-2">
        <i class="bi bi-arrow-left"></i> Back to List</a>
      <form method="POST" action="/quotations/{q["id"]}/delete"
            onsubmit="return confirm('Delete this quotation permanently?')">
        <button class="btn btn-outline-danger w-100"><i class="bi bi-trash"></i> Delete</button>
      </form>
    </div>
  </div>
</div>'''
        return self._layout(q['quotation_no'], q['quotation_no'], content, 'quotations')


