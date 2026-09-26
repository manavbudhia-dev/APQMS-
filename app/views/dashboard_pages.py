import json
from utils.http_utils import esc
from utils.http_utils import fmt_rupee
from database import get_db
from utils.dates import date_to_iso as _date_to_iso
from utils.dates import date_in_fy
from utils.dates import fy_start_year
from datetime import datetime

WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _pct_trend(cur, prev):
    """Percent change from `prev` to `cur` (e.g. this month vs last month)."""
    if prev > 0:
        return round(((cur - prev) / prev) * 100)
    return 100 if cur > 0 else 0


def _trend_html(pct):
    icon = 'up' if pct >= 0 else 'down'
    color = '#10B981' if pct >= 0 else '#DC2626'
    sign = '+' if pct >= 0 else ''
    return (f'<div class="stat-trend" style="color:{color}">'
            f'<i class="bi bi-arrow-{icon}"></i> {sign}{pct}% '
            f'<span class="trend-muted">vs last month</span></div>')


def _fy_period_html(fy):
    """Replaces the month-over-month trend badge when a past/future
    financial year is selected — "vs last month" doesn't mean anything
    there, so the stat card shows the FY's date range instead."""
    return (f'<div class="stat-trend"><span class="trend-muted">'
            f'FY {esc(fy["label"])} period</span></div>')


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _dashboard_row_html(r):
    """Render one <tr> for the dashboard's Recent Quotations table. Shared
    by the initial server render and the live filter API so the two never
    drift out of sync."""
    status_lower = esc(r["status"].lower())
    date_iso = _date_to_iso(r["date"])
    amount_html = f'Rs. {fmt_rupee(r["amount"])}' if r["amount"] else '<span class="text-muted">-</span>'
    return (
        f'<tr class="quotation-row" data-status="{status_lower}" data-date-iso="{date_iso}">'
        f'<td><strong>{esc(r["quotation_no"])}</strong></td>'
        f'<td>{esc(r["company_name"] or "-")}</td>'
        f'<td>{esc(r["date"])}</td>'
        f'<td><span class="badge badge-{status_lower}">{esc(r["status"])}</span></td>'
        f'<td class="text-end">{amount_html}</td>'
        f'<td class="text-end">'
        f'<div class="dropdown">'
        f'<button class="row-action-btn" type="button" data-bs-toggle="dropdown" '
        f'aria-expanded="false" title="Actions"><i class="bi bi-three-dots"></i></button>'
        f'<ul class="dropdown-menu dropdown-menu-end">'
        f'<li><a class="dropdown-item" href="/quotations/{r["id"]}"><i class="bi bi-eye"></i> View</a></li>'
        f'<li><a class="dropdown-item" href="/quotations/{r["id"]}/edit"><i class="bi bi-pencil"></i> Edit</a></li>'
        f'<li><a class="dropdown-item" href="/quotations/{r["id"]}/pdf"><i class="bi bi-file-earmark-pdf"></i> Download PDF</a></li>'
        f'</ul></div></td></tr>')


class DashboardPagesMixin:
    # Quotation statuses the dashboard filter accepts, keyed by the
    # lowercase value the <select> sends.
    _DASH_STATUS_MAP = {'draft': 'Draft', 'sent': 'Sent', 'confirmed': 'Confirmed', 'cancelled': 'Cancelled'}

    def _dashboard_quotations_fragment(self, q='', status='', date_from='', date_to='', limit=None):
        """Search/filter/date-range across ALL quotations (not just the
        dashboard's initial 5), for the live '/api/dashboard/quotations'
        endpoint. The stored date is free text ("DD - MM - YYYY"), not a
        real SQL date column, so status/search narrow the set in SQL and
        the date range is applied afterwards in Python via _date_to_iso —
        simplest correct option at this table's scale, and it reuses the
        exact same parser the initial page render already relies on."""
        q = (q or '').strip()
        date_from = (date_from or '').strip()
        date_to = (date_to or '').strip()
        status_db = self._DASH_STATUS_MAP.get((status or '').strip().lower(), '')

        try:
            limit = int(limit) if limit else 8
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 100))

        db = get_db()
        where, params = [], []
        if q:
            like = f"%{q}%"
            where.append("(q.quotation_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR q.date LIKE ?)")
            params.extend([like, like, like, like])
        if status_db:
            where.append("q.status = ?")
            params.append(status_db)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        rows = db.execute(
            "SELECT q.*, c.company_name, "
            "(SELECT COALESCE(SUM(qi.quantity*qi.rate),0) FROM quotation_items qi "
            " WHERE qi.quotation_id=q.id) AS amount "
            f"FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id{where_sql} "
            "ORDER BY q.created_at DESC", params).fetchall()
        db.close()

        # Scoped to the session's selected financial year, same as the
        # main quotations list — "All Financial Years" (fy is None)
        # leaves every quotation in.
        fy = self._current_fy()
        rows = [r for r in rows if date_in_fy(r['date'], fy)]

        if date_from or date_to:
            def _in_range(r):
                iso = _date_to_iso(r['date'])
                if not iso:
                    return False
                if date_from and iso < date_from:
                    return False
                if date_to and iso > date_to:
                    return False
                return True
            rows = [r for r in rows if _in_range(r)]

        total_matched = len(rows)
        shown_rows = rows[:limit]

        if shown_rows:
            rows_html = ''.join(_dashboard_row_html(r) for r in shown_rows)
        else:
            rows_html = ('<tr><td colspan="6" class="text-center text-muted py-4">'
                         'No quotations match your filters.</td></tr>')

        shown = len(shown_rows)
        count_label = (f'Showing {shown} of {total_matched} quotation{"s" if total_matched != 1 else ""}'
                       if total_matched else 'No quotations match your filters')

        return {'rows_html': rows_html, 'count_label': count_label,
                'total_matched': total_matched, 'shown': shown}

    # ── PAGE: DASHBOARD ──────────────────────────────────────
    def pg_dashboard(self, user_id):
        db = get_db()
        user_row = db.execute(
            "SELECT role, full_name FROM users WHERE id=?", (user_id,)).fetchone()
        role = user_row['role'] if user_row else 'user'
        full_name = (user_row['full_name'] if user_row and user_row['full_name'] else 'there')
        first_name = full_name.strip().split(' ')[0] if full_name.strip() else 'there'

        # Customers & items are shared master data — always shown in
        # full, regardless of the selected financial year.
        tc = db.execute("SELECT COUNT(*) c FROM customers").fetchone()['c']
        ti = db.execute("SELECT COUNT(*) c FROM items").fetchone()['c']

        # Quotations are scoped to the session's selected financial year
        # (None = "All Financial Years"). Pulled once, with each row's
        # computed line-item value, and reused below for every stat card,
        # the donut chart, the revenue figures and "Recent Quotations" —
        # the financial-year filter itself runs in Python since the
        # document date is free text, not a real SQL date column.
        fy = self._current_fy()
        all_quotations = db.execute(
            "SELECT q.*, c.company_name, "
            "(SELECT COALESCE(SUM(qi.quantity*qi.rate),0) FROM quotation_items qi "
            " WHERE qi.quotation_id=q.id) AS amount "
            "FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id "
            "ORDER BY q.created_at DESC").fetchall()
        db_quotations = [r for r in all_quotations if date_in_fy(r['date'], fy)]

        tq = len(db_quotations)
        dq = sum(1 for r in db_quotations if r['status'] == 'Draft')
        sq = sum(1 for r in db_quotations if r['status'] == 'Sent')
        cq = sum(1 for r in db_quotations if r['status'] == 'Confirmed')
        xq = sum(1 for r in db_quotations if r['status'] == 'Cancelled')

        now = datetime.now()
        current_month = now.strftime('%Y-%m')
        py, pm = _prev_month(now.year, now.month)
        prev_month = f"{py:04d}-{pm:02d}"

        # "vs last month" only makes sense while looking at "All
        # Financial Years" or the FY containing today — for a past/future
        # FY there's no meaningful "last month" to compare against, so
        # the trend badges are swapped for a plain FY-period caption.
        is_current_period = fy is None or fy['start_year'] == fy_start_year()

        # Customers/items trends are never FY-scoped (see above), so they
        # always use the plain month-over-month count.
        def _count_trend(table, extra_sql=''):
            cur = db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE created_at LIKE ?{extra_sql}",
                (f"{current_month}%",)).fetchone()['c']
            prev = db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE created_at LIKE ?{extra_sql}",
                (f"{prev_month}%",)).fetchone()['c']
            return _pct_trend(cur, prev)

        tc_trend = _count_trend('customers')
        ti_trend = _count_trend('items')
        db.close()

        # Quotation trends & revenue are derived from db_quotations (the
        # FY-scoped set) instead of separate SQL queries, so a selected
        # financial year narrows these figures exactly the way it
        # narrows the headline counts above.
        def _created_in(rows, month_key, status=None):
            return [r for r in rows if (r['created_at'] or '').startswith(month_key)
                    and (status is None or r['status'] == status)]

        if is_current_period:
            tq_trend = _pct_trend(len(_created_in(db_quotations, current_month)),
                                   len(_created_in(db_quotations, prev_month)))
            dq_trend = _pct_trend(len(_created_in(db_quotations, current_month, 'Draft')),
                                   len(_created_in(db_quotations, prev_month, 'Draft')))
            rev = sum(r['amount'] for r in _created_in(db_quotations, current_month))
            rev_prev = sum(r['amount'] for r in _created_in(db_quotations, prev_month))
            rev_trend = _pct_trend(rev, rev_prev)
            rev_caption = 'Total estimated value &middot; this month'
        else:
            tq_trend = dq_trend = rev_trend = 0
            rev = sum(r['amount'] for r in db_quotations)
            rev_caption = f'Total estimated value &middot; FY {esc(fy["label"])}'

        # "Quotation Value" sparkline: the last 6 calendar months when
        # looking at "All Financial Years" or the current FY (an
        # operational, always-recent pulse); the FY's own 12 months
        # (April - March), aggregated from the already FY-filtered
        # quotations above, when a specific past/future FY is selected.
        chart_labels, chart_values = [], []
        if is_current_period:
            offsets = []
            cy, cm = now.year, now.month
            for _ in range(6):
                offsets.append((cy, cm))
                cy, cm = _prev_month(cy, cm)
            for oy, om in reversed(offsets):
                key = f"{oy:04d}-{om:02d}"
                v = sum(r['amount'] for r in _created_in(all_quotations, key))
                chart_labels.append(MONTHS[om - 1])
                chart_values.append(round(v, 2))
        else:
            by_month = {}
            for r in db_quotations:
                iso = _date_to_iso(r['date'])
                if iso:
                    by_month[iso[:7]] = by_month.get(iso[:7], 0) + (r['amount'] or 0)
            sy = fy['start_year']
            for i in range(12):
                y, m = (sy, 4 + i) if i < 9 else (sy + 1, i - 8)
                key = f"{y:04d}-{m:02d}"
                chart_labels.append(MONTHS[m - 1])
                chart_values.append(round(by_month.get(key, 0), 2))

        # Quotation status breakdown, for the donut chart + legend.
        donut_labels = ['Draft', 'Sent', 'Confirmed']
        donut_values = [dq, sq, cq]
        donut_colors = ['#F59E0B', '#3B82F6', '#10B981']
        if xq > 0:
            donut_labels.append('Cancelled')
            donut_values.append(xq)
            donut_colors.append('#DC2626')

        # Recent quotations (within the selected financial year), with
        # each row's estimated value.
        recent = db_quotations[:5]

        rows = ''
        for r in recent:
            rows += _dashboard_row_html(r)
        if not recent:
            empty_msg = ('No quotations yet — create your first one to see it here.'
                         if not all_quotations else
                         f'No quotations in FY {esc(fy["label"])} yet.' if fy is not None else
                         'No quotations yet — create your first one to see it here.')
            rows = (f'<tr><td colspan="6" class="text-center text-muted py-4">'
                    f'{empty_msg}</td></tr>')

        greeting_hour = now.hour
        if greeting_hour < 12:
            greeting_word = 'morning'
        elif greeting_hour < 17:
            greeting_word = 'afternoon'
        else:
            greeting_word = 'evening'
        date_str = f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}"

        cancelled_row = ''
        if xq > 0:
            cancelled_row = (
                '<div class="legend-row"><span class="d-flex align-items-center gap-2">'
                '<span class="status-dot status-dot-cancelled"></span> Cancelled</span>'
                f'<strong>{xq}</strong></div>')

        shown = len(recent)

        content = f'''
<div class="dash-greeting mb-4">
  <div>
    <h4>&#128075; Good {greeting_word}, {esc(first_name)}</h4>
    <div class="dash-sub">Here's what's happening with your business today.</div>
  </div>
  <div class="dash-date"><strong>{date_str}</strong>Keep up the great work!</div>
</div>

<div class="row g-3 mb-4">
  <div class="col-md-3 col-6">
    <div class="stat-card">
      <div class="d-flex justify-content-between align-items-center">
        <div><div class="stat-value">{tq}</div><div class="stat-label">Quotations</div></div>
        <div class="stat-icon" style="background:#FEF2F2;color:#DC2626"><i class="bi bi-file-earmark-text"></i></div>
      </div>
      {_trend_html(tq_trend) if is_current_period else _fy_period_html(fy)}
    </div>
  </div>
  <div class="col-md-3 col-6">
    <div class="stat-card">
      <div class="d-flex justify-content-between align-items-center">
        <div><div class="stat-value">{tc}</div><div class="stat-label">Customers</div></div>
        <div class="stat-icon" style="background:#EFF6FF;color:#3B82F6"><i class="bi bi-people"></i></div>
      </div>
      {_trend_html(tc_trend)}
    </div>
  </div>
  <div class="col-md-3 col-6">
    <div class="stat-card">
      <div class="d-flex justify-content-between align-items-center">
        <div><div class="stat-value">{ti}</div><div class="stat-label">Items</div></div>
        <div class="stat-icon" style="background:#ECFDF5;color:#10B981"><i class="bi bi-box-seam"></i></div>
      </div>
      {_trend_html(ti_trend)}
    </div>
  </div>
  <div class="col-md-3 col-6">
    <div class="stat-card">
      <div class="d-flex justify-content-between align-items-center">
        <div><div class="stat-value">{dq}</div><div class="stat-label">Drafts</div></div>
        <div class="stat-icon" style="background:#FFFBEB;color:#F59E0B"><i class="bi bi-file-earmark-text"></i></div>
      </div>
      {_trend_html(dq_trend) if is_current_period else _fy_period_html(fy)}
    </div>
  </div>
</div>

<div class="row g-4">
  <div class="col-md-8">
    <div class="table-card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Recent Quotations{'' if fy is None else f' <span class="fy-badge">FY {esc(fy["label"])}</span>'}</span>
        <a href="/quotations" class="btn btn-sm btn-outline-primary">View All <i class="bi bi-chevron-right"></i></a>
      </div>
      <div class="px-3 pt-3 pb-2">
        <div class="table-toolbar">
          <div class="input-group input-group-sm" style="max-width:220px">
            <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
            <input type="text" id="quotationSearch" class="form-control border-start-0" placeholder="Search quotations">
          </div>
          <select id="quotationStatusFilter" class="form-select form-select-sm">
            <option value="">All Status</option>
            <option value="draft">Draft</option>
            <option value="sent">Sent</option>
            <option value="confirmed">Confirmed</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <div class="dropdown">
            <button type="button" class="icon-btn-sm" id="dashDateFilterBtn" data-bs-toggle="dropdown"
                    data-bs-auto-close="outside" aria-expanded="false" title="Filter by date range">
              <i class="bi bi-calendar3"></i>
            </button>
            <div class="dropdown-menu dropdown-menu-end p-3 dash-date-filter" aria-labelledby="dashDateFilterBtn">
              <div class="mb-2">
                <label class="form-label small mb-1">From</label>
                <input type="date" id="dashDateFrom" class="form-control form-control-sm">
              </div>
              <div class="mb-3">
                <label class="form-label small mb-1">To</label>
                <input type="date" id="dashDateTo" class="form-control form-control-sm">
              </div>
              <div class="d-flex gap-2">
                <button type="button" class="btn btn-outline-secondary btn-sm flex-grow-1" id="dashDateClear">Clear</button>
                <button type="button" class="btn btn-primary btn-sm flex-grow-1" id="dashDateApply">Apply</button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="table-responsive-wrapper">
      <table class="table mb-0" id="quotationsTable">
        <thead><tr><th>No.</th><th>Customer</th><th>Date</th><th>Status</th><th class="text-end">Amount</th><th class="text-end">Action</th></tr></thead>
        <tbody id="quotationsTableBody">{rows}</tbody>
      </table>
      </div>
      <div class="d-flex align-items-center justify-content-center text-muted py-2 border-top" style="font-size:12px">
        <span id="dashQuotationsCountLabel">Showing {shown} of {tq} quotation{'s' if tq != 1 else ''}</span>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="form-section h-100">
      <h5><i class="bi bi-lightning"></i> Quick Actions</h5>
      <a href="/quotations/new" class="btn btn-primary w-100 mb-2"><i class="bi bi-plus-circle"></i> New Quotation</a>
      <a href="/proforma/new" class="btn btn-outline-secondary w-100 mb-2"><i class="bi bi-receipt"></i> New Proforma Invoice</a>
      <a href="/customers" class="btn btn-outline-secondary w-100 mb-2"><i class="bi bi-people"></i> Manage Customers</a>
      <a href="/items" class="btn btn-outline-secondary w-100 mb-2"><i class="bi bi-box-seam"></i> Item Catalog</a>
    </div>
  </div>
</div>

<div class="row g-4 mt-1">
  <div class="col-md-4">
    <div class="table-card h-100">
      <div class="card-header"><span>Quotation Value</span></div>
      <div class="card-body">
        <div class="metric-figure">Rs. {fmt_rupee(rev)}</div>
        <div class="metric-caption">{rev_caption}</div>
        <div class="sparkline-wrap"><canvas id="revChart"></canvas></div>
        {_trend_html(rev_trend) if is_current_period else _fy_period_html(fy)}
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="table-card h-100">
      <div class="card-header"><span>Quotation Status</span></div>
      <div class="card-body">
        <div class="donut-wrap">
          <div class="donut-canvas-wrap">
            <canvas id="statusDonut"></canvas>
            <div class="donut-center"><strong>{tq}</strong><span>Total</span></div>
          </div>
          <div class="status-legend">
            <div class="legend-row"><span class="d-flex align-items-center gap-2"><span class="status-dot status-dot-draft"></span> Draft</span><strong>{dq}</strong></div>
            <div class="legend-row"><span class="d-flex align-items-center gap-2"><span class="status-dot status-dot-sent"></span> Sent</span><strong>{sq}</strong></div>
            <div class="legend-row"><span class="d-flex align-items-center gap-2"><span class="status-dot status-dot-confirmed"></span> Confirmed</span><strong>{cq}</strong></div>
            {cancelled_row}
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="promo-card">
      <div class="promo-icon"><i class="bi bi-rocket-takeoff"></i></div>
      <h6>Streamline your workflow</h6>
      <p>Create quotations, manage customers and grow your business faster.</p>
      <a href="/quotations/new" class="btn btn-primary btn-sm align-self-start"><i class="bi bi-plus-circle"></i> Create New Quotation</a>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script>
(function () {{
  var searchInput = document.getElementById('quotationSearch');
  var statusSelect = document.getElementById('quotationStatusFilter');
  var dateFrom = document.getElementById('dashDateFrom');
  var dateTo = document.getElementById('dashDateTo');
  var dateBtn = document.getElementById('dashDateFilterBtn');
  var dateApply = document.getElementById('dashDateApply');
  var dateClear = document.getElementById('dashDateClear');
  var tbody = document.getElementById('quotationsTableBody');
  var countLabel = document.getElementById('dashQuotationsCountLabel');
  var searchDebounce = null;
  var fetchSeq = 0;

  // Live server-side search: queries every quotation in the database,
  // not just the handful already rendered on the dashboard, so results
  // aren't capped by whatever happened to load on first paint.
  function fetchFiltered() {{
    var params = new URLSearchParams();
    var term = (searchInput && searchInput.value || '').trim();
    var status = (statusSelect && statusSelect.value || '');
    var from = (dateFrom && dateFrom.value) || '';
    var to = (dateTo && dateTo.value) || '';
    if (term) params.set('q', term);
    if (status) params.set('status', status);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);

    var seq = ++fetchSeq;
    fetch('/api/dashboard/quotations?' + params.toString())
      .then(function (res) {{ return res.json(); }})
      .then(function (data) {{
        if (seq !== fetchSeq || !tbody) return; // a newer request already landed
        tbody.innerHTML = data.rows_html;
        if (countLabel) countLabel.textContent = data.count_label;
      }})
      .catch(function () {{
        if (seq !== fetchSeq || !countLabel) return;
        countLabel.textContent = 'Could not load quotations — please try again.';
      }});
  }}

  searchInput && searchInput.addEventListener('input', function () {{
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(fetchFiltered, 300);
  }});
  statusSelect && statusSelect.addEventListener('change', fetchFiltered);

  function updateDateButtonState() {{
    if (!dateBtn) return;
    var active = !!((dateFrom && dateFrom.value) || (dateTo && dateTo.value));
    dateBtn.classList.toggle('active', active);
    dateBtn.title = active ? 'Date range applied — click to change' : 'Filter by date range';
  }}
  dateApply && dateApply.addEventListener('click', function () {{
    fetchFiltered();
    updateDateButtonState();
    var dd = bootstrap.Dropdown.getOrCreateInstance(dateBtn);
    dd.hide();
  }});
  dateClear && dateClear.addEventListener('click', function () {{
    if (dateFrom) dateFrom.value = '';
    if (dateTo) dateTo.value = '';
    fetchFiltered();
    updateDateButtonState();
    var dd = bootstrap.Dropdown.getOrCreateInstance(dateBtn);
    dd.hide();
  }});

  if (window.Chart) {{
    var revCtx = document.getElementById('revChart');
    if (revCtx) {{
      new Chart(revCtx, {{
        type: 'line',
        data: {{
          labels: {json.dumps(chart_labels)},
          datasets: [{{
            data: {json.dumps(chart_values)},
            borderColor: '#DC2626',
            backgroundColor: 'rgba(220,38,38,0.08)',
            borderWidth: 2, fill: true, tension: 0.4,
            pointRadius: 0, pointHoverRadius: 4, pointBackgroundColor: '#DC2626'
          }}]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: function (ctx) {{
              return 'Rs. ' + Number(ctx.parsed.y).toLocaleString('en-IN');
            }} }} }}
          }},
          scales: {{ x: {{ display: false }}, y: {{ display: false }} }},
          interaction: {{ intersect: false, mode: 'index' }}
        }}
      }});
    }}
    var donutCtx = document.getElementById('statusDonut');
    if (donutCtx) {{
      new Chart(donutCtx, {{
        type: 'doughnut',
        data: {{
          labels: {json.dumps(donut_labels)},
          datasets: [{{ data: {json.dumps(donut_values)}, backgroundColor: {json.dumps(donut_colors)}, borderWidth: 0 }}]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false, cutout: '72%',
          plugins: {{ legend: {{ display: false }} }}
        }}
      }});
    }}
  }}
}})();
</script>'''
        return self._layout('Dashboard', 'Dashboard', content, 'dashboard')


    # ── PAGE: 404 ────────────────────────────────────────────
    def pg_404(self):
        content = ('<div class="empty-state"><i class="bi bi-exclamation-triangle"></i>'
                   '<h3>404</h3><p>Page not found</p>'
                   '<a href="/dashboard" class="btn btn-primary">Go to Dashboard</a></div>')
        return self._layout('404', 'Page Not Found', content, '')
