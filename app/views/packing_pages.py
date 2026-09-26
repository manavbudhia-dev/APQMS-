import json
import math
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import redirect
from utils.http_utils import pdf_response
from database import get_db
from database import gen_packing_list_no
from utils.dates import date_in_fy
from datetime import datetime


class PackingPagesMixin:
    PACKING_STATUSES = ['Draft', 'Sent', 'Confirmed', 'Cancelled']
    PACKING_PAGE_SIZE = 10

    # ── PAGE: PACKING LISTS ──────────────────────────────────
    # Layout below is a deliberate structural replica of the Proforma
    # Invoices list page (proforma_pages.py: _proforma_rows_html /
    # _proforma_fragment / pg_pilist) — same stat cards, search +
    # status-chip + date-filter toolbar, checkbox/row-numbering table,
    # pagination + page-size control, and "Quick Tips" card — with the
    # invoice-only column (Order No.) swapped for the packing-list
    # equivalent (the linked Proforma Invoice, if any).
    def _packing_rows_html(self, rows_data, start_index=1):
        """Render <tr> rows for a set of packing list records (already
        fetched). `start_index` is the running row number of the first
        row on this page (i.e. the offset + 1), so numbering stays
        continuous across pages instead of restarting at 1 every time."""
        rows = ''
        for i, p in enumerate(rows_data):
            n = start_index + i
            customer_label = p['company_name'] or p['contact_person'] or '-'
            rows += (f'<tr>'
                     f'<td><input type="checkbox" class="form-check-input q-row-check" value="{p["id"]}"></td>'
                     f'<td class="text-muted">{n}</td>'
                     f'<td><strong>{esc(p["pl_no"])}</strong></td>'
                     f'<td>{esc(p["linked_pi_no"] or "-")}</td>'
                     f'<td>{esc(p["date"])}</td>'
                     f'<td>{esc(customer_label)}</td>'
                     f'<td><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td>'
                     f'<td class="text-end">'
                     f'<a href="/packing/{p["id"]}" class="row-action-btn me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/packing/{p["id"]}/pdf" class="row-action-btn me-1 ra-pdf" title="Download PDF"><i class="bi bi-file-earmark-pdf"></i></a>'
                     f'<a href="/packing/{p["id"]}/edit" class="row-action-btn me-1 ra-edit" title="Edit"><i class="bi bi-pencil"></i></a>'
                     f'<form method="POST" action="/packing/{p["id"]}/delete" class="d-inline" '
                     f'onsubmit="return confirm(\'Delete packing list {esc(p["pl_no"])} permanently?\')">'
                     f'<button type="submit" class="row-action-btn ra-delete" title="Delete"><i class="bi bi-trash"></i></button></form>'
                     f'</td></tr>')
        return rows

    def _packing_empty_state(self, has_any, q, status, fy=None):
        """Illustration + message for zero results, whether the table is
        truly empty or just has no matches for the current search/filter
        (which now includes the selected financial year)."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-box-seam"></i></div>'
                    '<h6>No packing lists yet</h6>'
                    '<p class="text-muted">Once you create a packing list, it will show up here.</p>'
                    '<a href="/packing/new" class="btn btn-primary btn-sm">'
                    '<i class="bi bi-plus-circle"></i> Create your first packing list</a>'
                    '</div>')
        fy_note = f' in FY {esc(fy["label"])}' if fy is not None else ''
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching packing lists</h6>'
                '<p class="text-muted">Try a different search term'
                + (f' or clear the "{esc(status)}" filter' if status else '')
                + fy_note + '.'
                + (' Switch financial year in the dropdown at the top of the page to see other years.' if fy is not None else '')
                + '</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="plClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _packing_pagination_html(self, page, total_pages, q, status):
        html = '<nav aria-label="Packing invoices pagination"><ul class="pagination pagination-sm q-pagination mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="plGoToPage(event,{p})">{label}</a></li>')

        html += page_item(page - 1, '&laquo;', disabled=(page <= 1))
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

    def _packing_chips_html(self, status_counts, all_count, status):
        chips = [('', 'All', all_count)] + [(s, s, status_counts.get(s, 0)) for s in self.PACKING_STATUSES]
        html = ''
        for val, label, count in chips:
            active = ' active' if val == status else ''
            html += (f'<button type="button" class="q-chip{active}" data-status="{esc(val)}" '
                     f'onclick="plSetStatus(\'{esc(val)}\')">{esc(label)} '
                     f'<span class="q-chip-count">{count}</span></button>')
        return html

    def _packing_stat_cards_html(self, status_counts, all_count):
        """Five summary cards (Total / Draft / Sent / Confirmed /
        Cancelled) shown above the packing lists table — same shape
        and colour scheme as the proforma invoices list."""
        cards = [
            ('Total Packing Lists', all_count, 'bi-box-seam', '#3B82F6', '#EFF6FF'),
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

    def _packing_fragment(self, q='', status='', page='1', page_size=None):
        """Fetch, filter, and paginate packing lists; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/filter/pagination without a full reload.

        Scoped to the session's currently-selected financial year (see
        BaseMixin._current_fy) unless "All Financial Years" is selected.
        The packing-list date is free text, not a real SQL date column,
        so that part of the filtering happens in Python rather than SQL."""
        q = (q or '').strip()
        status = status or ''
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        if status not in self.PACKING_STATUSES:
            status = ''

        try:
            page_size = int(page_size) if page_size else self.PACKING_PAGE_SIZE
        except (TypeError, ValueError):
            page_size = self.PACKING_PAGE_SIZE
        if page_size not in (10, 25, 50, 100):
            page_size = self.PACKING_PAGE_SIZE

        fy = self._current_fy()

        db = get_db()

        # Linked proforma invoice (pi.pi_no) is joined in everywhere the
        # customer is, both so it can be searched on and so it can be
        # shown as the packing-list equivalent of the proforma's "Order
        # No." column.
        join_sql = (" FROM packing_lists p "
                    "LEFT JOIN customers c ON p.customer_id=c.id "
                    "LEFT JOIN proforma_invoices pi ON p.pi_id=pi.id")

        search_where, search_params = [], []
        if q:
            like = f"%{q}%"
            search_where.append(
                "(p.pl_no LIKE ? OR pi.pi_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR p.date LIKE ?)")
            search_params.extend([like, like, like, like, like])
        search_sql = (" WHERE " + " AND ".join(search_where)) if search_where else ""

        # Whether the table has ANY data at all (ignoring filters) —
        # decides which empty-state message to show.
        has_any = db.execute("SELECT COUNT(*) c FROM packing_lists").fetchone()['c'] > 0

        # Every packing list matching the search term (all statuses) —
        # the financial-year filter is applied here in Python, then
        # status counts/pagination are derived from this filtered set.
        search_rows = db.execute(
            f"SELECT p.*,c.company_name,c.contact_person,pi.pi_no AS linked_pi_no{join_sql}{search_sql} "
            f"ORDER BY p.created_at DESC", search_params).fetchall()
        db.close()
        search_rows = [r for r in search_rows if date_in_fy(r['date'], fy)]

        all_count = len(search_rows)
        status_counts = {s: 0 for s in self.PACKING_STATUSES}
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
            rows_html = self._packing_rows_html(rows_data, start_index=offset + 1)
        else:
            rows_html = f'<tr><td colspan="8" class="p-0">{self._packing_empty_state(has_any, q, status, fy)}</td></tr>'

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + page_size, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total} packing lists'
                       if total else 'No packing lists found')

        return {
            'rows_html': rows_html,
            'pagination_html': self._packing_pagination_html(page, total_pages, q, status),
            'chips_html': self._packing_chips_html(status_counts, all_count, status),
            'stat_cards_html': self._packing_stat_cards_html(status_counts, all_count),
            'count_label': count_label,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        }

    def pg_pllist(self, q='', status='', page='1', page_size=None):
        frag = self._packing_fragment(q=q, status=status, page=page, page_size=page_size)
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
#packingTbody {{ transition:opacity .1s; }}
#packingTbody.q-loading {{ opacity:.4; }}

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
    <div class="q-page-head-icon"><i class="bi bi-box-seam"></i></div>
    <div>
      <h1>All Packing Lists</h1>
      <p>View and manage all customer packing lists</p>
    </div>
  </div>
  <a href="/packing/new" class="btn btn-primary"><i class="bi bi-plus-circle"></i> New Packing List</a>
</div>

<div class="row g-3 mb-3" id="plStatCards">{frag["stat_cards_html"]}</div>

<div class="table-card">
  <div class="p-3 border-bottom">
    <div class="q-toolbar">
      <div class="input-group input-group-sm q-search-wrap">
        <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
        <input type="text" id="plSearchInput" class="form-control border-start-0" placeholder="Search by PL no., linked PI, customer or date..." value="{esc(q)}">
      </div>
      <button type="button" class="btn btn-outline-secondary btn-sm q-date-btn" title="Filter by date">
        <i class="bi bi-calendar3"></i> Select Date
        <input type="date" id="plDateInput" aria-label="Select date">
      </button>
      <div class="q-chips" id="plChips">{frag["chips_html"]}</div>
      <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" id="plExportBtn"><i class="bi bi-download"></i> Export</button>
    </div>
  </div>
  <div class="table-responsive-wrapper">
  <table class="table mb-0">
    <thead>
      <tr>
        <th style="width:36px"><input type="checkbox" class="form-check-input" id="plSelectAll"></th>
        <th style="width:40px">#</th>
        <th>PL No.</th>
        <th>Linked PI</th>
        <th class="q-th-sort">Date <i class="bi bi-arrow-down-up"></i></th>
        <th class="q-th-sort">Customer <i class="bi bi-arrow-down-up"></i></th>
        <th>Status</th>
        <th class="text-end">Actions</th>
      </tr>
    </thead>
    <tbody id="packingTbody">{frag["rows_html"]}</tbody>
  </table>
  </div>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="plCountLabel">{frag["count_label"]}</span>
    <div class="d-flex align-items-center gap-2">
      <div id="plPagination">{frag["pagination_html"]}</div>
      <select class="q-pagesize-select" id="plPageSize">{size_opts}</select>
    </div>
  </div>
</div>

<div class="q-tips-card">
  <div class="q-tips-icon"><i class="bi bi-info-lg"></i></div>
  <div>
    <h6>Quick Tips</h6>
    <ul>
      <li>Create a new packing list to send with a customer's shipment.</li>
      <li>Import items straight from a proforma invoice instead of adding them manually.</li>
      <li>Use filters to quickly find packing lists by status or date.</li>
      <li>You can view, edit or download a packing list as PDF from the actions menu.</li>
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
    var tbody = document.getElementById('packingTbody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    params.set('page', state.page);
    params.set('page_size', state.pageSize);
    fetch('/api/packing?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('plPagination').innerHTML = data.pagination_html;
        document.getElementById('plChips').innerHTML = data.chips_html;
        document.getElementById('plCountLabel').innerHTML = data.count_label;
        document.getElementById('plStatCards').innerHTML = data.stat_cards_html;
        document.getElementById('plSelectAll').checked = false;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.status) url.searchParams.set('status', state.status); else url.searchParams.delete('status');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        if (state.pageSize !== 10) url.searchParams.set('page_size', state.pageSize); else url.searchParams.delete('page_size');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.plGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.plSetStatus = function(s) {{
    state.status = s;
    state.page = 1;
    refresh();
  }};

  window.plClearFilters = function() {{
    state.q = '';
    state.status = '';
    state.page = 1;
    document.getElementById('plSearchInput').value = '';
    document.getElementById('plDateInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('plSearchInput');
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

  document.getElementById('plPageSize').addEventListener('change', function(e) {{
    state.pageSize = parseInt(e.target.value, 10) || 10;
    state.page = 1;
    refresh();
  }});

  var plDateInput = document.getElementById('plDateInput');
  plDateInput.addEventListener('click', function(e) {{
    // The native input is stretched invisibly over the whole button, but
    // browsers only auto-open the calendar when the click lands exactly on
    // the tiny native icon hotspot. Force it open on any click instead.
    if (typeof this.showPicker === 'function') {{
      try {{ this.showPicker(); }} catch (err) {{ /* ignore */ }}
    }}
  }});
  plDateInput.addEventListener('change', function(e) {{
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

  document.getElementById('plSelectAll').addEventListener('change', function(e) {{
    document.querySelectorAll('.q-row-check').forEach(function(cb) {{ cb.checked = e.target.checked; }});
  }});

  document.getElementById('plExportBtn').addEventListener('click', function() {{
    var rows = [['PL No.', 'Linked PI', 'Date', 'Customer', 'Status']];
    document.querySelectorAll('#packingTbody tr').forEach(function(tr) {{
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
    link.download = 'packing_lists.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }});
}})();
</script>'''
        return self._layout('Packing Lists', 'All Packing Lists', content, 'packing')


    def pg_plnew(self):
        db = get_db()
        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        # Fetch all proforma invoices for the dropdown
        profs = db.execute(
            "SELECT pi.*, c.company_name FROM proforma_invoices pi "
            "LEFT JOIN customers c ON pi.customer_id = c.id "
            "ORDER BY pi.created_at DESC").fetchall()
        plno = gen_packing_list_no(db)
        today = datetime.now().strftime('%d - %m - %Y')
        db.close()

        cust_opts = '<option value="">- Select Customer -</option>'
        for c in customers:
            cust_opts += (f'<option value="{c["id"]}" '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        item_opts = '<option value="">- Pick an item -</option>'
        for i in items:
            item_opts += (f'<option value="{i["id"]}" '
                          f'data-name="{esc(i["item_name"])}" '
                          f'data-desc="{esc(i["description"] or "")}" '
                          f'data-spec="{esc(i["specification"] or "")}" '
                          f'data-unit="{esc(i["unit"])}">'
                          f'{esc(i["item_name"])} ({esc(i["unit"])})</option>')

        prof_opts = '<option value="">- Select Proforma Invoice -</option>'
        for p in profs:
            label = f'{p["pi_no"]}'
            if p['company_name']:
                label += f' ({p["company_name"]})'
            label += f' - {p["date"]}'
            prof_opts += f'<option value="{p["id"]}" data-customer="{p["customer_id"] or ""}">{esc(label)}</option>'

        js = """
let plItems = [];
let plCounter = 0;
const PL_EMPTY_HTML = '<div class="pl-empty"><i class="bi bi-inbox"></i><strong>No items added yet.</strong><span>Import from a proforma invoice or add items manually above.</span></div>';

function fillCustPL() {
  const sel = document.getElementById('custSelPL');
  const info = document.getElementById('custInfoPL');
  if (sel.value) {
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePL').textContent = opt.dataset.name;
    document.getElementById('cCompanyPL').textContent = opt.dataset.company;
    document.getElementById('cAddrPL').textContent = opt.dataset.address;
    info.style.display = 'block';
  } else {
    info.style.display = 'none';
  }
}

function loadProformaItems() {
  const sel = document.getElementById('profSelPL');
  const piId = sel.value;
  const btn = document.getElementById('btnLoadProf');
  const msg = document.getElementById('profMsg');
  if (!piId) { msg.textContent = ''; return; }
  msg.innerHTML = '<span class="text-muted"><i class="bi bi-arrow-clockwise"></i> Loading items...</span>';
  // Store pi_id in hidden field
  document.getElementById('piIdPL').value = piId;
  fetch('/api/proforma/' + piId + '/items')
    .then(r => r.json())
    .then(data => {
      if (data.items && data.items.length > 0) {
        // clear current items
        plItems = [];
        data.items.forEach(function(it) {
          plCounter++;
          plItems.push({
            id: plCounter,
            itemId: it.item_id || '',
            name: it.item_name || '',
            desc: it.description || '',
            spec: it.specification || '',
            unit: it.unit || 'Pcs',
            qty: it.quantity || 0,
            bag: 0
          });
        });
        // auto-select customer from proforma
        if (data.customer_id) {
          document.getElementById('custSelPL').value = data.customer_id;
          fillCustPL();
        }
        renderItemsPL();
        msg.innerHTML = '<span class="text-success"><i class="bi bi-check-circle"></i> ' + data.items.length + ' items loaded from proforma. Enter Qty/Bag for each item below.</span>';
      } else {
        msg.innerHTML = '<span class="text-warning">No items found in this proforma invoice.</span>';
      }
    })
    .catch(err => {
      msg.innerHTML = '<span class="text-danger">Error loading items. Please try again.</span>';
    });
}

function addItemPL() {
  const sel = document.getElementById('itemSelPL');
  const qty = parseFloat(document.getElementById('itemQtyPL').value);
  const bag = parseFloat(document.getElementById('itemBagPL').value);
  if (!sel.value) { alert('Please select an item.'); return; }
  if (!qty || qty <= 0) { alert('Please enter total quantity.'); return; }
  if (!bag || bag <= 0) { alert('Please enter quantity per bag.'); return; }
  const opt = sel.options[sel.selectedIndex];
  plCounter++;
  plItems.push({
    id: plCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc,
    spec: opt.dataset.spec,
    unit: opt.dataset.unit,
    qty: qty,
    bag: bag
  });
  renderItemsPL();
  sel.value = '';
  document.getElementById('itemQtyPL').value = '';
  document.getElementById('itemBagPL').value = '';
}

function removeItemPL(id) {
  plItems = plItems.filter(function(i) { return i.id !== id; });
  renderItemsPL();
}

function updateBagQty(id, val) {
  const item = plItems.find(function(i) { return i.id === id; });
  if (item) {
    item.bag = parseFloat(val) || 0;
    // Update hidden input and display text without full re-render
    const hidden = document.querySelector('input[name="bag_qty[]"][value="' + 0 + '"]');
    // Update hidden inputs for this item
    document.querySelectorAll('#itemsListPL .quotation-item-row').forEach(function(row, idx) {
      if (plItems[idx] && plItems[idx].id === id) {
        const hiddens = row.querySelectorAll('input[type="hidden"]');
        if (hiddens[2]) hiddens[2].value = item.bag;
        const infoDiv = row.querySelector('small.text-muted:last-of-type, small.text-warning');
        if (infoDiv) {
          const totalBags = item.bag > 0 ? Math.ceil(item.qty / item.bag) : 0;
          const fullBags = item.bag > 0 ? Math.floor(item.qty / item.bag) : 0;
          const lastBag = item.bag > 0 ? (item.qty - fullBags * item.bag) : 0;
          let breakdown = '';
          if (item.bag > 0 && item.qty > 0) {
            if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
            if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
            else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
            if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
          }
          if (item.bag > 0) {
            infoDiv.className = 'text-muted';
            infoDiv.innerHTML = 'Bags: <strong>' + totalBags + '</strong>' + (breakdown ? ' (' + breakdown + ')' : '');
          } else {
            infoDiv.className = 'text-warning';
            infoDiv.textContent = 'Enter Qty/Bag';
          }
        }
      }
    });
    updSummaryPL();
  }
}

function updateTotalQty(id, val) {
  const item = plItems.find(function(i) { return i.id === id; });
  if (item) {
    item.qty = parseFloat(val) || 0;
    // Update hidden input for quantity
    document.querySelectorAll('#itemsListPL .quotation-item-row').forEach(function(row, idx) {
      if (plItems[idx] && plItems[idx].id === id) {
        const hiddens = row.querySelectorAll('input[type="hidden"]');
        if (hiddens[1]) hiddens[1].value = item.qty;
        const infoDiv = row.querySelector('small.text-muted:last-of-type, small.text-warning');
        if (infoDiv && item.bag > 0) {
          const totalBags = Math.ceil(item.qty / item.bag);
          const fullBags = Math.floor(item.qty / item.bag);
          const lastBag = item.qty - fullBags * item.bag;
          let breakdown = '';
          if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
          if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
          else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
          if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
          infoDiv.innerHTML = 'Bags: <strong>' + totalBags + '</strong> (' + breakdown + ')';
        }
      }
    });
    updSummaryPL();
  }
}

function renderItemsPL() {
  const list = document.getElementById('itemsListPL');
  if (plItems.length === 0) {
    list.innerHTML = PL_EMPTY_HTML;
    list.classList.remove('has-items');
    updSummaryPL();
    return;
  }
  let html = '';
  plItems.forEach(function(item, idx) {
    const totalBags = item.bag > 0 ? Math.ceil(item.qty / item.bag) : 0;
    const fullBags = item.bag > 0 ? Math.floor(item.qty / item.bag) : 0;
    const lastBag = item.bag > 0 ? (item.qty - fullBags * item.bag) : 0;
    let breakdown = '';
    if (item.bag > 0 && item.qty > 0) {
      if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
      if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
      else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
      if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
    }
    html += `
    <div class="quotation-item-row">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1">
          <span class="item-name">${idx + 1}. ${item.name}</span>
          <input type="hidden" name="item_id[]" value="${item.itemId}">
          <input type="hidden" name="quantity[]" value="${item.qty}">
          <input type="hidden" name="bag_qty[]" value="${item.bag}">
          <div class="item-spec">${(item.spec || '').replace(/\\n/g, '<br>')}</div>
          <div class="row g-1 mt-1 align-items-center">
            <div class="col-auto"><small class="text-muted">Total Qty:</small></div>
            <div class="col-auto"><input type="number" step="0.01" min="0" class="form-control form-control-sm" style="width:90px" value="${item.qty}" onchange="updateTotalQty(${item.id}, this.value)"></div>
            <div class="col-auto"><small class="text-muted">${item.unit}</small></div>
            <div class="col-auto"><small class="text-muted">|</small></div>
            <div class="col-auto"><small class="text-muted">Qty/Bag:</small></div>
            <div class="col-auto"><input type="number" step="0.01" min="0" class="form-control form-control-sm" style="width:90px" value="${item.bag}" placeholder="0" onchange="updateBagQty(${item.id}, this.value)"></div>
            <div class="col-auto"><small class="text-muted">${item.unit}</small></div>
          </div>
          ${item.bag > 0 ? `<small class="text-muted">Bags: <strong>${totalBags}</strong> ${breakdown ? '(' + breakdown + ')' : ''}</small>` : '<small class="text-warning">Enter Qty/Bag</small>'}
        </div>
        <div class="text-end">
          <button type="button" class="btn btn-sm btn-outline-danger mt-1" onclick="removeItemPL(${item.id})"><i class="bi bi-x-circle"></i> Remove</button>
        </div>
      </div>
    </div>`;
  });
  list.innerHTML = html;
  list.classList.add('has-items');
  updSummaryPL();
}

function updSummaryPL() {
  const countEl = document.getElementById('sItemCount');
  const bagEl = document.getElementById('sBagCount');
  if (!countEl || !bagEl) return;
  let bags = 0;
  plItems.forEach(function(item) {
    bags += item.bag > 0 ? Math.ceil(item.qty / item.bag) : (item.qty > 0 ? 1 : 0);
  });
  countEl.textContent = plItems.length;
  bagEl.textContent = bags;
  const note = document.getElementById('plSummaryNote');
  if (note) {
    if (plItems.length === 0) {
      note.className = 'pl-note pl-note-empty';
      note.innerHTML = '<i class="bi bi-info-circle"></i><div><strong>No items added yet.</strong><span>Add items to see the summary details here.</span></div>';
    } else {
      note.className = 'pl-note pl-note-ok';
      note.innerHTML = '<i class="bi bi-check-circle"></i><div><strong>' + plItems.length + (plItems.length === 1 ? ' item' : ' items') + ' added.</strong><span>' + bags + (bags === 1 ? ' bag' : ' bags') + ' to pack in this shipment.</span></div>';
    }
  }
}

function submitFormPL() {
  // Validate that all items have bag qty set
  for (let i = 0; i < plItems.length; i++) {
    if (!plItems[i].bag || plItems[i].bag <= 0) {
      alert('Please enter Qty/Bag for item: ' + plItems[i].name);
      return false;
    }
    if (!plItems[i].qty || plItems[i].qty <= 0) {
      alert('Please enter Total Qty for item: ' + plItems[i].name);
      return false;
    }
  }
  if (plItems.length === 0) {
    alert('Please add at least one item to the packing list.');
    return false;
  }
  return true;
}
"""

        def step(n, icon, title, sub, body):
            return (f'<section class="pl-card pl-step">'
                    f'<span class="pl-num">{n}</span>'
                    f'<div class="pl-step-main">'
                    f'<header class="pl-step-head"><i class="bi {icon}"></i>'
                    f'<div><h2>{title}</h2><p>{sub}</p></div></header>'
                    f'<div class="pl-step-body">{body}</div>'
                    f'</div></section>')

        s1 = f'''
<div class="row g-3">
  <div class="col-md-4"><label class="pl-label">PL No</label>
    <input class="pl-input pl-readonly" value="{esc(plno)}" readonly></div>
  <div class="col-md-4"><label class="pl-label">Date</label>
    <div class="pl-field"><i class="bi bi-calendar3"></i>
      <input name="date" class="pl-input" value="{esc(today)}"></div></div>
  <div class="col-md-4"><label class="pl-label">Status</label>
    <div class="pl-input pl-static"><span class="pl-dot"></span>Draft<i class="bi bi-chevron-down"></i></div></div>
</div>'''

        s2 = f'''
<label class="pl-label">Select Proforma Invoice</label>
<div class="pl-row">
  <div class="pl-field pl-grow"><i class="bi bi-file-earmark"></i>
    <select class="pl-input pl-select" id="profSelPL">{prof_opts}</select></div>
  <button type="button" class="pl-btn pl-btn-green" id="btnLoadProf" onclick="loadProformaItems()"><i class="bi bi-download"></i> Import Items</button>
</div>
<div id="profMsg" class="pl-msg"></div>'''

        s3 = f'''
<label class="pl-label">Select Customer</label>
<div class="pl-field pl-plain-select">
  <select name="customer_id" class="pl-input pl-select" id="custSelPL" onchange="fillCustPL()">{cust_opts}</select></div>
<div id="custInfoPL" class="pl-cust-info" style="display:none">
  <strong id="cNamePL"></strong>
  <span id="cCompanyPL"></span>
  <span id="cAddrPL" style="white-space:pre-line"></span>
</div>'''

        s4 = f'''
<div class="pl-add-grid">
  <div><label class="pl-label">Select Item</label>
    <div class="pl-field"><i class="bi bi-box"></i>
      <select class="pl-input pl-select" id="itemSelPL">{item_opts}</select></div></div>
  <div><label class="pl-label">Total Qty</label>
    <div class="pl-field"><i class="bi bi-hash"></i>
      <input type="number" step="0.01" min="0" class="pl-input" id="itemQtyPL" placeholder="0" data-enter-action="addItemPL()"></div></div>
  <div><label class="pl-label">Qty / Bag</label>
    <div class="pl-field"><i class="bi bi-bag"></i>
      <input type="number" step="0.01" min="0" class="pl-input" id="itemBagPL" placeholder="0" data-enter-action="addItemPL()"></div></div>
  <button type="button" class="pl-btn pl-btn-red" onclick="addItemPL()"><i class="bi bi-plus-lg"></i> Add item</button>
</div>'''

        s5 = '''
<div id="itemsListPL" class="pl-items-box">
  <div class="pl-empty"><i class="bi bi-inbox"></i><strong>No items added yet.</strong>
  <span>Import from a proforma invoice or add items manually above.</span></div>
</div>'''

        s6 = '''
<div class="pl-field pl-notes"><i class="bi bi-pencil"></i>
  <textarea name="notes" class="pl-input" rows="3" placeholder="Optional notes..."></textarea></div>'''

        content = f'''
<div class="pl-page">
<div class="pl-head">
  <div class="pl-head-left">
    <i class="bi bi-box-seam"></i>
    <div>
      <h1>Create New Packing List</h1>
      <p>Create a new packing list for your customer's shipment</p>
    </div>
  </div>
  <a href="/packing" class="pl-btn pl-btn-ghost"><i class="bi bi-arrow-left"></i> Back to Packing Lists</a>
</div>

<form method="POST" action="/packing/new" onsubmit="return submitFormPL()">
  <div class="pl-layout">
    <div class="pl-main">
      {step(1, 'bi-box-seam', 'Packing List Info', 'Enter basic details for this packing list', s1)}
      {step(2, 'bi-file-earmark-text', 'Import from Proforma Invoice (Optional)', 'Import items directly from an existing proforma invoice to save time', s2)}
      {step(3, 'bi-person', 'Customer', 'Select the customer for this packing list', s3)}
      {step(4, 'bi-cart-plus', 'Add Item Manually', 'Add items to packing list manually', s4)}
      {step(5, 'bi-list-ul', 'Items in Packing List', 'Review and manage all items in this packing list', s5)}
      {step(6, 'bi-journal-text', 'Notes', 'Add any additional notes (optional)', s6)}
    </div>

    <aside class="pl-side">
      <section class="pl-card pl-side-card">
        <header class="pl-step-head"><i class="bi bi-bar-chart-line"></i>
          <div><h2>Summary</h2><p>Current status and item count</p></div></header>
        <div class="pl-tiles">
          <div class="pl-tile pl-tile-items"><span class="pl-tile-icon"><i class="bi bi-box-seam"></i></span>
            <span class="pl-tile-label">Items</span><b id="sItemCount">0</b></div>
          <div class="pl-tile pl-tile-bags"><span class="pl-tile-icon"><i class="bi bi-bag"></i></span>
            <span class="pl-tile-label">Total Bags</span><b id="sBagCount">0</b></div>
        </div>
        <div id="plSummaryNote" class="pl-note pl-note-empty"><i class="bi bi-info-circle"></i>
          <div><strong>No items added yet.</strong><span>Add items to see the summary details here.</span></div></div>
      </section>

      <section class="pl-card pl-side-card">
        <header class="pl-step-head"><i class="bi bi-check-circle-fill"></i>
          <div><h2>Create Packing List</h2><p>Review all information and create the packing list</p></div></header>
        <input type="hidden" name="pi_id" id="piIdPL" value="">
        <button type="submit" class="pl-btn pl-btn-red pl-block" title="Save (Ctrl+S)"><i class="bi bi-check-circle"></i> Create Packing List</button>
        <a href="/packing" class="pl-btn pl-btn-ghost pl-block"><i class="bi bi-x-circle"></i> Cancel</a>
      </section>
    </aside>
  </div>
</form>
</div>
<script>{js}</script>'''
        return self._layout(f'New {plno}', 'Create Packing List', content, 'packing',
                            crumbs=[('Packing Lists', '/packing'), ('Create New', None)])


    def handle_packing_create(self, form, user_id):
        db = get_db()
        plno = gen_packing_list_no(db)
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None
        try:
            pi_id = int(form.get('pi_id')) if form.get('pi_id') else None
        except (TypeError, ValueError):
            pi_id = None
        cur = db.execute(
            "INSERT INTO packing_lists "
            "(pl_no,pi_id,date,customer_id,subject,status,notes,created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (plno, pi_id, date, customer_id, form.get('subject', ''),
             'Draft', form.get('notes', ''), user_id))
        plid = cur.lastrowid

        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        bag_qtys = form.getlist('bag_qty[]')
        for i, (iid, qty, bagq) in enumerate(zip(item_ids, quantities, bag_qtys)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
                bagq = float(bagq) if bagq else qty
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO packing_list_items "
                    "(packing_list_id,item_id,item_name,description,specification,"
                    "total_qty,unit,bag_qty,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (plid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'], bagq, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/packing/{plid}'))


    def handle_packing_edit(self, plid, form, user_id):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.send(*redirect('/packing'))

        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        db.execute(
            "UPDATE packing_lists SET date=?, customer_id=?, "
            "subject=?, notes=? WHERE id=?",
            (date, customer_id, form.get('subject', ''),
             form.get('notes', ''), plid))

        db.execute("DELETE FROM packing_list_items WHERE packing_list_id=?", (plid,))
        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        bag_qtys = form.getlist('bag_qty[]')
        for i, (iid, qty, bagq) in enumerate(zip(item_ids, quantities, bag_qtys)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
                bagq = float(bagq) if bagq else qty
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO packing_list_items "
                    "(packing_list_id,item_id,item_name,description,specification,"
                    "total_qty,unit,bag_qty,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (plid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'], bagq, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/packing/{plid}'))


    # ── PAGE: PACKING LIST DETAIL ────────────────────────────
    # Two-column layout mirrors proforma_pages.pg_pidetail: a printable
    # letterhead-style document on the left (same header treatment as
    # packing_generator.build_packing_pdf, so the on-screen preview
    # matches the PDF) and a Details / Summary / Actions sidebar on the
    # right, in place of the proforma's Details / Financial Summary /
    # Actions sidebar.
    def pg_pldetail(self, plid):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()
        c = None
        if p['customer_id']:
            c = db.execute("SELECT * FROM customers WHERE id=?",
                           (p['customer_id'],)).fetchone()
        pi = None
        if p['pi_id']:
            pi = db.execute("SELECT pi_no FROM proforma_invoices WHERE id=?",
                            (p['pi_id'],)).fetchone()
        items = db.execute(
            "SELECT * FROM packing_list_items WHERE packing_list_id=? "
            "ORDER BY sort_order", (plid,)).fetchall()
        db.close()

        def _fmt_qty(qty):
            return str(int(qty)) if qty == int(qty) else str(qty)

        item_rows = ''
        qty_by_unit = {}
        total_bags_sum = 0
        for idx, it in enumerate(items, 1):
            total_qty = it['total_qty'] or 0
            bag_qty = it['bag_qty'] or 0
            if bag_qty > 0:
                total_bags = math.ceil(total_qty / bag_qty)
                full_bags = int(total_qty // bag_qty)
                last_bag = total_qty - (full_bags * bag_qty)
            else:
                total_bags = 1
                full_bags = 0
                last_bag = total_qty

            breakdown_parts = []
            if full_bags > 0:
                breakdown_parts.append(f"{full_bags} x {_fmt_qty(bag_qty)}")
            if last_bag > 0 and last_bag != bag_qty:
                breakdown_parts.append(f"1 x {_fmt_qty(last_bag)}")
            elif last_bag > 0 and full_bags == 0:
                breakdown_parts.append(f"1 x {_fmt_qty(last_bag)}")
            breakdown = ' + '.join(breakdown_parts) if breakdown_parts else _fmt_qty(total_qty)

            unit = it['unit'] or ''
            qty_by_unit[unit] = qty_by_unit.get(unit, 0) + total_qty
            total_bags_sum += total_bags

            spec = esc(it['specification'] or '').replace('&#10;', '<br>')
            item_rows += (f'<tr><td class="text-center"><strong>{idx}</strong></td>'
                          f'<td><strong>{esc(it["item_name"])}</strong></td>'
                          f'<td style="font-size:11px">{esc_nl(it["description"] or "")}'
                          f'{("<br><br><b>Specification:</b><br>" + spec) if spec else ""}</td>'
                          f'<td class="text-center">{_fmt_qty(total_qty)}</td>'
                          f'<td class="text-center">{esc(unit)}</td>'
                          f'<td class="text-center">{_fmt_qty(bag_qty)}</td>'
                          f'<td class="text-center"><strong>{total_bags}</strong></td>'
                          f'<td class="text-center" style="font-size:11px">{esc(breakdown)}</td></tr>')
        if not items:
            item_rows = ('<tr><td colspan="8" class="text-center text-muted py-4">'
                         'No items in this packing list.</td></tr>')

        qty_total_str = ', '.join(f"{_fmt_qty(q)} {esc(u)}" for u, q in qty_by_unit.items()) or '-'

        cName = esc(c['contact_person']) if c else ''
        cCompany = esc(c['company_name']) if c else ''
        cAddr = esc_nl(c['address'] or '') if c else ''
        cGstin = esc(c['gstin'] or '') if c else ''

        linked_pi_html = '-'
        if pi:
            linked_pi_html = f'<a href="/proforma/{p["pi_id"]}">{esc(pi["pi_no"])}</a>'

        status_opts = ''
        for s in self.PACKING_STATUSES:
            sel = ' selected' if p['status'] == s else ''
            status_opts += f'<option value="{s}"{sel}>{s}</option>'

        notes_html = ''
        if p['notes']:
            notes_html = (f'<div class="mt-3" style="background:#FFF9DB;border:1px solid #FFD43B;'
                          f'border-radius:8px;padding:10px 14px;font-size:12px">'
                          f'<b>Note:</b> {esc_nl(p["notes"])}</div>')

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

      <div style="margin-top:16px"><b>M/s.</b><br>
        <b>{cCompany}</b><br>{cAddr}<br><small>{cGstin}</small>
      </div>

      <div class="d-flex justify-content-between align-items-center my-3">
        <b>Ref. Proforma: {linked_pi_html if pi else "-"}</b>
        <b style="text-decoration:underline">PACKING LIST</b>
      </div>

      <table class="table table-bordered mt-3" style="font-size:12px">
        <thead style="background:#E2E8F0">
          <tr>
            <th style="width:5%">Sr.</th><th style="width:14%">Item</th>
            <th style="width:22%">Description &amp; Specification</th>
            <th style="width:9%">Total Qty</th><th style="width:7%">Unit</th>
            <th style="width:9%">Qty/Bag</th><th style="width:7%">Bags</th>
            <th style="width:22%">Bag Breakdown</th>
          </tr>
        </thead>
        <tbody>{item_rows}</tbody>
      </table>

      <div class="text-end" style="font-size:13px">
        <div><b>Total Quantity: {qty_total_str}</b></div>
        <div style="font-size:17px;color:#DC2626;border-top:2px solid #333;padding-top:4px;margin-top:4px">
          <b>Total No. of Bags/Packages: {total_bags_sum}</b></div>
      </div>
      {notes_html}

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
        <tr><td class="text-muted">No</td><td class="text-end"><b>{esc(p["pl_no"])}</b></td></tr>
        <tr><td class="text-muted">Linked PI</td><td class="text-end">{linked_pi_html}</td></tr>
        <tr><td class="text-muted">Date</td><td class="text-end">{esc(p["date"])}</td></tr>
        <tr><td class="text-muted">Status</td>
          <td class="text-end"><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td></tr>
        <tr><td class="text-muted">Customer</td><td class="text-end">{cCompany or "-"}</td></tr>
        <tr><td class="text-muted">Created</td><td class="text-end">{esc(p["created_at"] or "-")}</td></tr>
      </table>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-calculator"></i> Summary</h5>
      <div class="summary-box">
        <div class="total-line"><span>Items</span><b>{len(items)}</b></div>
        <div class="total-line"><span>Total Quantity</span><b>{qty_total_str}</b></div>
        <div class="total-line grand-total"><span>Total Bags</span><b>{total_bags_sum}</b></div>
      </div>
    </div>

    <div class="form-section">
      <h5><i class="bi bi-gear"></i> Actions</h5>
      <a href="/packing/{p["id"]}/pdf" class="btn btn-danger w-100 mb-2">
        <i class="bi bi-file-earmark-pdf"></i> Download PDF</a>
      <a href="/packing/{p["id"]}/edit" class="btn btn-primary w-100 mb-2">
        <i class="bi bi-pencil"></i> Edit Packing List</a>
      <form method="POST" action="/packing/{p["id"]}/status" class="mb-2">
        <label class="form-label">Change Status</label>
        <select name="status" class="form-select form-select-sm" onchange="this.form.submit()">{status_opts}</select>
      </form>
      <a href="/packing" class="btn btn-outline-primary w-100 mb-2">
        <i class="bi bi-arrow-left"></i> Back to List</a>
      <form method="POST" action="/packing/{p["id"]}/delete"
            onsubmit="return confirm('Delete this packing list permanently?')">
        <button class="btn btn-outline-danger w-100"><i class="bi bi-trash"></i> Delete</button>
      </form>
    </div>
  </div>
</div>'''
        return self._layout(p['pl_no'], p['pl_no'], content, 'packing')


    def pg_pledit(self, plid):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()

        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        pl_items = db.execute(
            "SELECT * FROM packing_list_items WHERE packing_list_id=? ORDER BY sort_order",
            (plid,)).fetchall()
        db.close()

        cust_opts = '<option value="">- Select Customer -</option>'
        for c in customers:
            sel = ' selected' if p['customer_id'] == c['id'] else ''
            cust_opts += (f'<option value="{c["id"]}"{sel} '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        item_opts = '<option value="">- Pick an item -</option>'
        for i in items:
            item_opts += (f'<option value="{i["id"]}" '
                          f'data-name="{esc(i["item_name"])}" '
                          f'data-desc="{esc(i["description"] or "")}" '
                          f'data-spec="{esc(i["specification"] or "")}" '
                          f'data-unit="{esc(i["unit"])}">'
                          f'{esc(i["item_name"])} ({esc(i["unit"])})</option>')

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

        existing_items_js = '['
        for idx, it in enumerate(pl_items):
            if idx > 0:
                existing_items_js += ','
            existing_items_js += '{'
            existing_items_js += f'id: {idx + 1}, '
            existing_items_js += f'itemId: {it["item_id"]}, '
            existing_items_js += f'name: "{_js_str(it["item_name"])}", '
            existing_items_js += f'desc: "{_js_str(it["description"])}", '
            existing_items_js += f'spec: "{_js_str(it["specification"])}", '
            existing_items_js += f'unit: "{_js_str(it["unit"])}", '
            existing_items_js += f'qty: {it["total_qty"]}, '
            existing_items_js += f'bag: {it["bag_qty"]}'
            existing_items_js += '}'
        existing_items_js += ']'

        js = f"""
let plItems = {existing_items_js};
let plCounter = {len(pl_items)};

function fillCustPL() {{
  const sel = document.getElementById('custSelPL');
  const info = document.getElementById('custInfoPL');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePL').textContent = opt.dataset.name;
    document.getElementById('cCompanyPL').textContent = opt.dataset.company;
    document.getElementById('cAddrPL').textContent = opt.dataset.address;
    info.style.display = 'block';
  }} else {{
    info.style.display = 'none';
  }}
}}

function addItemPL() {{
  const sel = document.getElementById('itemSelPL');
  const qty = parseFloat(document.getElementById('itemQtyPL').value);
  const bag = parseFloat(document.getElementById('itemBagPL').value);
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  if (!qty || qty <= 0) {{ alert('Please enter total quantity.'); return; }}
  if (!bag || bag <= 0) {{ alert('Please enter quantity per bag.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  plCounter++;
  plItems.push({{
    id: plCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc,
    spec: opt.dataset.spec,
    unit: opt.dataset.unit,
    qty: qty,
    bag: bag
  }});
  renderItemsPL();
  sel.value = '';
  document.getElementById('itemQtyPL').value = '';
  document.getElementById('itemBagPL').value = '';
}}

function removeItemPL(id) {{
  plItems = plItems.filter(function(i) {{ return i.id !== id; }});
  renderItemsPL();
}}

function renderItemsPL() {{
  const list = document.getElementById('itemsListPL');
  if (plItems.length === 0) {{
    list.innerHTML = PL_EMPTY_HTML;
    list.classList.remove('has-items');
    updSummaryPL();
    return;
  }}
  let html = '';
  plItems.forEach(function(item, idx) {{
    const totalBags = Math.ceil(item.qty / item.bag);
    html += `
    <div class="quotation-item-row">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="item-name">${{idx + 1}}. ${{item.name}}</span>
          <input type="hidden" name="item_id[]" value="${{item.itemId}}">
          <input type="hidden" name="quantity[]" value="${{item.qty}}">
          <input type="hidden" name="bag_qty[]" value="${{item.bag}}">
          <div class="item-spec">${{item.spec.replace(/\\n/g, '<br>')}}</div>
          <small class="text-muted">Total: ${{item.qty}} ${{item.unit}} | Per Bag: ${{item.bag}} ${{item.unit}} | Bags: ${{totalBags}}</small>
        </div>
        <div class="text-end">
          <button type="button" class="btn btn-sm btn-outline-danger mt-1" onclick="removeItemPL(${{item.id}})"><i class="bi bi-x-circle"></i> Remove</button>
        </div>
      </div>
    </div>`;
  }});
  list.innerHTML = html;
  list.classList.add('has-items');
  updSummaryPL();
}}

function updSummaryPL() {{
  const countEl = document.getElementById('sItemCount');
  const bagEl = document.getElementById('sBagCount');
  if (!countEl || !bagEl) return;
  let bags = 0;
  plItems.forEach(function(item) {{
    bags += item.bag > 0 ? Math.ceil(item.qty / item.bag) : (item.qty > 0 ? 1 : 0);
  }});
  countEl.textContent = plItems.length;
  bagEl.textContent = bags;
}}

function submitFormPL() {{
  if (plItems.length === 0) {{
    alert('Please add at least one item to the packing list.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{ renderItemsPL(); fillCustPL(); }});
"""

        content = f'''
<div class="page-head">
  <div class="page-head-left">
    <i class="bi bi-box-seam"></i>
    <div>
      <h1>Edit {esc(p["pl_no"])}</h1>
      <p>Update packing list details and items</p>
    </div>
  </div>
  <a href="/packing/{plid}" class="btn btn-outline-secondary"><i class="bi bi-arrow-left"></i> Back to Packing List</a>
</div>

<form method="POST" action="/packing/{plid}/edit" onsubmit="return submitFormPL()">
  <div class="row g-3">
    <div class="col-lg-8">
      <div class="form-section mb-3">
        <h5><i class="bi bi-info-circle"></i> Edit Packing List</h5>
        <div class="row g-3">
          <div class="col-md-4"><label class="form-label">PL No.</label>
            <input type="text" class="form-control" value="{esc(p['pl_no'])}" readonly></div>
          <div class="col-md-4"><label class="form-label">Date</label>
            <input name="date" class="form-control" value="{esc(p['date'])}"></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-person"></i> Customer</h5>
        <select name="customer_id" class="form-select" id="custSelPL" onchange="fillCustPL()">{cust_opts}</select>
        <div id="custInfoPL" class="alert alert-info mt-3" style="display:{'block' if p['customer_id'] else 'none'}">
          <strong id="cNamePL"></strong><br>
          <span id="cCompanyPL"></span><br>
          <span id="cAddrPL" style="white-space:pre-line"></span>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-cart-plus"></i> Add Items</h5>
        <div class="row g-2 mb-3">
          <div class="col-md-4"><label class="form-label">Select Item</label>
            <select class="form-select" id="itemSelPL">{item_opts}</select></div>
          <div class="col-md-3"><label class="form-label">Total Qty</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemQtyPL" placeholder="0" data-enter-action="addItemPL()"></div>
          <div class="col-md-3"><label class="form-label">Qty / Bag</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemBagPL" placeholder="0" data-enter-action="addItemPL()"></div>
          <div class="col-md-2"><label class="form-label">&nbsp;</label>
            <button type="button" class="btn btn-primary w-100" onclick="addItemPL()"><i class="bi bi-plus"></i> Add</button></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-list-ol"></i> Items in Packing List</h5>
        <div id="itemsListPL"></div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-text-left"></i> Notes</h5>
        <textarea name="notes" class="form-control" rows="3" placeholder="Optional notes">{esc(p['notes'] or '')}</textarea>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="form-section mb-3">
        <h5><i class="bi bi-calculator"></i> Summary</h5>
        <div class="summary-box">
          <div class="total-line"><span>Items</span><b id="sItemCount">0</b></div>
          <div class="total-line grand-total"><span>Total Bags</span><b id="sBagCount">0</b></div>
        </div>
      </div>
      <div class="form-section">
        <button type="submit" class="btn btn-primary w-100 mb-2" title="Save (Ctrl+S)"><i class="bi bi-check-circle"></i> Update Packing List</button>
        <a href="/packing/{plid}" class="btn btn-outline-secondary w-100"><i class="bi bi-x-circle"></i> Cancel</a>
      </div>
    </div>
  </div>
</form>
<script>{js}</script>'''
        return self._layout(f'Edit {p["pl_no"]}', f'Edit Packing List - {p["pl_no"]}', content, 'packing')


    def _gen_pl_pdf(self, plid):
        from app.generators.packing_generator import build_packing_pdf
        db = get_db()
        row = db.execute("SELECT * FROM packing_lists WHERE id=?",
                         (plid,)).fetchone()
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
            "SELECT * FROM packing_list_items WHERE packing_list_id=? "
            "ORDER BY sort_order", (plid,)).fetchall()]
        db.close()
        pdf = build_packing_pdf(p, c, items)
        return pdf_response(pdf, f"{p['pl_no']}.pdf")


