import json
from utils.http_utils import esc
from utils.http_utils import fmt_rupee
from database import get_db
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


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


class DashboardPagesMixin:
    # ── PAGE: DASHBOARD ──────────────────────────────────────
    def pg_dashboard(self, user_id):
        db = get_db()
        user_row = db.execute(
            "SELECT role, full_name FROM users WHERE id=?", (user_id,)).fetchone()
        role = user_row['role'] if user_row else 'user'
        full_name = (user_row['full_name'] if user_row and user_row['full_name'] else 'there')
        first_name = full_name.strip().split(' ')[0] if full_name.strip() else 'there'

        tq = db.execute("SELECT COUNT(*) c FROM quotations").fetchone()['c']
        tc = db.execute("SELECT COUNT(*) c FROM customers").fetchone()['c']
        ti = db.execute("SELECT COUNT(*) c FROM items").fetchone()['c']
        dq = db.execute("SELECT COUNT(*) c FROM quotations WHERE status='Draft'").fetchone()['c']
        sq = db.execute("SELECT COUNT(*) c FROM quotations WHERE status='Sent'").fetchone()['c']
        cq = db.execute("SELECT COUNT(*) c FROM quotations WHERE status='Confirmed'").fetchone()['c']
        xq = db.execute("SELECT COUNT(*) c FROM quotations WHERE status='Cancelled'").fetchone()['c']

        now = datetime.now()
        current_month = now.strftime('%Y-%m')
        py, pm = _prev_month(now.year, now.month)
        prev_month = f"{py:04d}-{pm:02d}"

        # Month-over-month deltas for the trend line under each stat card.
        def _count_trend(table, extra_sql=''):
            cur = db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE created_at LIKE ?{extra_sql}",
                (f"{current_month}%",)).fetchone()['c']
            prev = db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE created_at LIKE ?{extra_sql}",
                (f"{prev_month}%",)).fetchone()['c']
            return _pct_trend(cur, prev)

        tq_trend = _count_trend('quotations')
        tc_trend = _count_trend('customers')
        ti_trend = _count_trend('items')
        dq_trend = _count_trend('quotations', " AND status='Draft'")

        # Revenue: this month vs last month (for the trend indicator)
        rev = db.execute(
            "SELECT COALESCE(SUM(qi.quantity*qi.rate),0) t FROM quotation_items qi "
            "JOIN quotations q ON qi.quotation_id=q.id "
            "WHERE q.created_at LIKE ?", (f"{current_month}%",)).fetchone()['t']
        rev_prev = db.execute(
            "SELECT COALESCE(SUM(qi.quantity*qi.rate),0) t FROM quotation_items qi "
            "JOIN quotations q ON qi.quotation_id=q.id "
            "WHERE q.created_at LIKE ?", (f"{prev_month}%",)).fetchone()['t']
        rev_trend = _pct_trend(rev, rev_prev)

        # Last 6 months of revenue, for the "Quotation Value" sparkline.
        chart_labels, chart_values = [], []
        yy, mm = now.year, now.month
        offsets = []
        cy, cm = yy, mm
        for _ in range(6):
            offsets.append((cy, cm))
            cy, cm = _prev_month(cy, cm)
        for oy, om in reversed(offsets):
            key = f"{oy:04d}-{om:02d}"
            v = db.execute(
                "SELECT COALESCE(SUM(qi.quantity*qi.rate),0) t FROM quotation_items qi "
                "JOIN quotations q ON qi.quotation_id=q.id "
                "WHERE q.created_at LIKE ?", (f"{key}%",)).fetchone()['t']
            chart_labels.append(MONTHS[om - 1])
            chart_values.append(round(v, 2))

        # Quotation status breakdown, for the donut chart + legend.
        donut_labels = ['Draft', 'Sent', 'Confirmed']
        donut_values = [dq, sq, cq]
        donut_colors = ['#F59E0B', '#3B82F6', '#10B981']
        if xq > 0:
            donut_labels.append('Cancelled')
            donut_values.append(xq)
            donut_colors.append('#DC2626')

        # Recent quotations, with each row's estimated value.
        recent = db.execute(
            "SELECT q.*, c.company_name, "
            "(SELECT COALESCE(SUM(qi.quantity*qi.rate),0) FROM quotation_items qi "
            " WHERE qi.quotation_id=q.id) AS amount "
            "FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id "
            "ORDER BY q.created_at DESC LIMIT 5").fetchall()
        db.close()

        rows = ''
        for r in recent:
            status_lower = esc(r["status"].lower())
            amount_html = f'Rs. {fmt_rupee(r["amount"])}' if r["amount"] else '<span class="text-muted">-</span>'
            rows += (
                f'<tr class="quotation-row" data-status="{status_lower}">'
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
        if not recent:
            rows = ('<tr><td colspan="6" class="text-center text-muted py-4">'
                    'No quotations yet — create your first one to see it here.'
                    '</td></tr>')

        # Everyone can change their own password; only admins can grant
        # other people access to the system.
        account_actions = '<a href="/change-password" class="btn btn-outline-secondary w-100 mb-2"><i class="bi bi-key"></i> Change Password</a>'
        if role == 'admin':
            account_actions += '<a href="/register" class="btn btn-outline-secondary w-100 mb-2"><i class="bi bi-person-plus"></i> Add User</a>'

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
      {_trend_html(tq_trend)}
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
      {_trend_html(dq_trend)}
    </div>
  </div>
</div>

<div class="row g-4">
  <div class="col-md-8">
    <div class="table-card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Recent Quotations</span>
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
          <button type="button" class="icon-btn-sm" style="cursor:not-allowed" disabled title="Date range filter (coming soon)"><i class="bi bi-calendar3"></i></button>
        </div>
      </div>
      <div class="table-responsive-wrapper">
      <table class="table mb-0" id="quotationsTable">
        <thead><tr><th>No.</th><th>Customer</th><th>Date</th><th>Status</th><th class="text-end">Amount</th><th class="text-end">Action</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
      <div class="d-flex align-items-center justify-content-center text-muted py-2 border-top" style="font-size:12px">
        Showing {shown} of {tq} quotation{'s' if tq != 1 else ''}
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
      {account_actions}
    </div>
  </div>
</div>

<div class="row g-4 mt-1">
  <div class="col-md-4">
    <div class="table-card h-100">
      <div class="card-header"><span>Quotation Value</span></div>
      <div class="card-body">
        <div class="metric-figure">Rs. {fmt_rupee(rev)}</div>
        <div class="metric-caption">Total estimated value &middot; this month</div>
        <div class="sparkline-wrap"><canvas id="revChart"></canvas></div>
        {_trend_html(rev_trend)}
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
  function applyFilter() {{
    var term = (searchInput && searchInput.value || '').toLowerCase();
    var status = (statusSelect && statusSelect.value || '').toLowerCase();
    document.querySelectorAll('#quotationsTable .quotation-row').forEach(function (row) {{
      var matchesText = row.textContent.toLowerCase().includes(term);
      var matchesStatus = !status || row.dataset.status === status;
      row.style.display = (matchesText && matchesStatus) ? '' : 'none';
    }});
  }}
  searchInput && searchInput.addEventListener('input', applyFilter);
  statusSelect && statusSelect.addEventListener('change', applyFilter);

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
