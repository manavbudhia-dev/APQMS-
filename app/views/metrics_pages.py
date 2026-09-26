"""
Business Metrics — /metrics

Three scikit-learn-backed views over the app's own data, each degrading
gracefully rather than failing outright:
  * Sales & Demand Forecast  — next-3-months trend from confirmed
                                proforma-invoice value and quotation volume
  * Pricing Suggestions      — items whose recent quoted rates trend away
                                from the current catalog rate
  * Attendance/Salary Anomalies — employee-months whose attendance,
                                overtime or advance pattern stands out

Any logged-in user can view this page (same as the Dashboard). Only an
admin can trigger a recompute (POST /metrics/refresh) — training,
however small, is real CPU work, and unlike a simple form save it isn't
something an ordinary action should be able to trigger repeatedly.
Results are cached to disk (app/ml/_cache/*.json — see app/ml/common.py)
so viewing the page never itself triggers training.

If scikit-learn isn't installed on this server, the page says so plainly
and explains how to enable it, instead of erroring.
"""
import json
import time
from datetime import datetime

from utils.http_utils import esc, fmt_rupee, html_response, redirect
from utils.http_utils import new_csrf_token, consume_csrf_token
from app.ml.common import (
    load_cache, save_cache, sklearn_available,
    seconds_since_last_refresh, MIN_REFRESH_INTERVAL_SECONDS,
)
from app.ml.forecasting import compute_forecast
from app.ml.pricing import compute_pricing_suggestions
from app.ml.anomalies import compute_anomalies
from app.ml.common import MLUnavailableError


def _month_label(yyyy_mm):
    try:
        return datetime.strptime(yyyy_mm, '%Y-%m').strftime('%b %Y')
    except ValueError:
        return esc(yyyy_mm)


def _when(ts):
    if not ts:
        return 'never'
    return datetime.fromtimestamp(ts).strftime('%d %b %Y, %I:%M %p')


def _trend_badge(trend):
    return {
        'up': '<span class="badge" style="background:#ECFDF5;color:#10B981"><i class="bi bi-arrow-up"></i> Trending up</span>',
        'down': '<span class="badge" style="background:#FEF2F2;color:#DC2626"><i class="bi bi-arrow-down"></i> Trending down</span>',
        'flat': '<span class="badge" style="background:#F1F5F9;color:#64748B"><i class="bi bi-dash"></i> Steady</span>',
    }.get(trend, '')


def _forecast_series_html(series, canvas_id, color, value_fmt):
    """One chart card for a forecast series dict (see forecasting.py's
    _build_result). History and forecast are drawn as one continuous
    line, with the forecast portion visually distinguished via a second
    dashed dataset that starts where history ends — Chart.js has no
    "half solid half dashed" line, so this is the standard way to fake
    it: two datasets sharing the same x-axis labels, one array padded
    with nulls where the other has data."""
    label = esc(series['label'])
    status = series['status']

    if status == 'no_data':
        body = '<div class="empty-state py-4"><p class="text-muted mb-0">No data yet.</p></div>'
        return f'''<div class="table-card h-100"><div class="card-header"><span>{label}</span></div>
<div class="card-body">{body}</div></div>'''

    history = series['history']
    forecast = series.get('forecast', [])
    all_months = [h['month'] for h in history] + [f['month'] for f in forecast]
    history_values = [h['value'] for h in history] + [None] * len(forecast)
    # Forecast line starts at the last real history point so it visually
    # connects rather than floating disconnected from where history ends.
    forecast_values = [None] * (len(history) - 1) + [history[-1]['value']] + \
        [f['value'] for f in forecast] if forecast else []

    labels_json = json.dumps([_month_label(m) for m in all_months])
    hist_json = json.dumps(history_values)
    fcst_json = json.dumps(forecast_values)

    note = ''
    if status == 'insufficient_data':
        note = f'<div class="text-muted small mt-2"><i class="bi bi-info-circle"></i> {esc(series.get("note", ""))}</div>'
    elif status == 'model_error':
        note = f'<div class="text-muted small mt-2"><i class="bi bi-exclamation-triangle"></i> {esc(series.get("note", ""))}</div>'

    trend_html = _trend_badge(series.get('trend')) if status == 'ok' else ''

    return f'''<div class="table-card h-100">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>{label}</span>{trend_html}
  </div>
  <div class="card-body">
    <div class="sparkline-wrap" style="height:220px"><canvas id="{canvas_id}"></canvas></div>
    {note}
  </div>
  <script>
  (function() {{
    if (!window.Chart) return;
    var el = document.getElementById('{canvas_id}');
    if (!el) return;
    new Chart(el, {{
      type: 'line',
      data: {{
        labels: {labels_json},
        datasets: [
          {{ label: 'History', data: {hist_json}, borderColor: '{color}',
             backgroundColor: '{color}22', borderWidth: 2, fill: true,
             tension: 0.35, pointRadius: 2, spanGaps: false }},
          {{ label: 'Forecast', data: {fcst_json}, borderColor: '{color}',
             borderWidth: 2, borderDash: [6,4], fill: false,
             tension: 0.35, pointRadius: 2, spanGaps: false }}
        ]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ y: {{ beginAtZero: true }} }},
        interaction: {{ intersect: false, mode: 'index' }}
      }}
    }});
  }})();
  </script>
</div>'''


def _pricing_table_html(pricing):
    status = pricing.get('status')
    if status in ('insufficient_data', 'model_error'):
        return (f'<div class="empty-state py-4"><i class="bi bi-tags"></i>'
                f'<p class="text-muted mb-0">{esc(pricing.get("note", ""))}</p></div>')

    rows = pricing.get('suggestions', [])
    if not rows:
        return '<div class="empty-state py-4"><p class="text-muted mb-0">No pricing trends to show yet.</p></div>'

    body_rows = []
    for s in rows:
        delta = s['delta_pct']
        color = '#DC2626' if delta > 0 else ('#10B981' if delta < 0 else '#64748B')
        sign = '+' if delta > 0 else ''
        body_rows.append(
            f'<tr><td>{esc(s["item_name"])}</td>'
            f'<td class="text-end">Rs. {fmt_rupee(s["catalog_rate"])}</td>'
            f'<td class="text-end">Rs. {fmt_rupee(s["suggested_rate"])}</td>'
            f'<td class="text-end" style="color:{color}">{sign}{delta}%</td>'
            f'<td class="text-end text-muted">{s["history_points"]}</td></tr>'
        )
    return f'''<div class="table-responsive-wrapper">
<table class="table mb-0">
  <thead><tr><th>Item</th><th class="text-end">Catalog Rate</th>
  <th class="text-end">Suggested Rate</th><th class="text-end">Change</th>
  <th class="text-end">Quotes Used</th></tr></thead>
  <tbody>{''.join(body_rows)}</tbody>
</table></div>
<div class="text-muted small p-3 border-top"><i class="bi bi-info-circle"></i>
Directional trend estimates from historical quoted rates — not guaranteed
values. Based on {pricing.get("items_considered", 0)} item(s) with enough
quote history to trend.</div>'''


def _anomalies_table_html(anomalies):
    status = anomalies.get('status')
    if status == 'insufficient_data':
        return (f'<div class="empty-state py-4"><i class="bi bi-shield-check"></i>'
                f'<p class="text-muted mb-0">{esc(anomalies.get("note", ""))}</p></div>')

    rows = anomalies.get('anomalies', [])
    method_note = ('Using a statistical fallback check — '
                    + esc(anomalies.get('note', ''))
                    if anomalies.get('method') == 'statistical' and anomalies.get('note')
                    else '')

    if not rows:
        empty = '<div class="empty-state py-4"><i class="bi bi-shield-check"></i><p class="text-muted mb-0">No anomalies flagged — attendance and advances look consistent with the usual pattern.</p></div>'
        return empty + (f'<div class="text-muted small px-3 pb-3">{method_note}</div>' if method_note else '')

    body_rows = []
    for a in rows:
        reasons = ', '.join(esc(r) for r in a['reasons'])
        method_badge = ('<span class="badge" style="background:#EFF6FF;color:#3B82F6">Neural</span>'
                         if a.get('method') == 'neural'
                         else '<span class="badge" style="background:#F1F5F9;color:#64748B">Statistical</span>')
        body_rows.append(
            f'<tr><td>{esc(a["employee_name"])}</td>'
            f'<td>{_month_label(a["month"])}</td>'
            f'<td>{reasons}</td>'
            f'<td class="text-end">{a["severity"]}</td>'
            f'<td class="text-end">{method_badge}</td></tr>'
        )
    table = f'''<div class="table-responsive-wrapper">
<table class="table mb-0">
  <thead><tr><th>Employee</th><th>Month</th><th>Why it was flagged</th>
  <th class="text-end">Severity</th><th class="text-end">Method</th></tr></thead>
  <tbody>{''.join(body_rows)}</tbody>
</table></div>'''
    if method_note:
        table += f'<div class="text-muted small p-3 border-top"><i class="bi bi-info-circle"></i> {method_note}</div>'
    return table


class BusinessMetricsPagesMixin:

    # ── PAGE: Business Metrics ──────────────────────────────
    def pg_metrics(self, user_id, msg='', err=False, success=False):
        is_admin = self._is_admin(user_id)

        if not sklearn_available():
            content = f'''
<div class="table-card">
  <div class="card-body text-center py-5">
    <i class="bi bi-cpu" style="font-size:2.5rem;color:#94A3B8"></i>
    <h5 class="mt-3">scikit-learn isn't installed on this server</h5>
    <p class="text-muted mx-auto" style="max-width:520px">
      Business Metrics (sales &amp; demand forecasting, pricing suggestions,
      and attendance anomaly detection) needs the optional ML dependencies.
      Install them with:
    </p>
    <code style="background:#F1F5F9;padding:6px 12px;border-radius:6px;display:inline-block">
      pip install -r requirements-ml.txt</code>
    <p class="text-muted mt-3 mb-0">The rest of the app works normally without this.</p>
  </div>
</div>'''
            return self._layout('Business Metrics', 'Business Metrics', content, 'metrics')

        forecast = load_cache('forecast')
        pricing = load_cache('pricing')
        anomalies = load_cache('anomalies')
        never_computed = not (forecast or pricing or anomalies)

        alert = ''
        if success:
            alert = '<div class="alert alert-success py-2 mb-3"><i class="bi bi-check-circle"></i> Metrics recalculated.</div>'
        elif err:
            alert = f'<div class="alert alert-danger py-2 mb-3"><i class="bi bi-exclamation-triangle"></i> {esc(msg)}</div>'

        last_updated = None
        for c in (forecast, pricing, anomalies):
            if c and c.get('computed_at'):
                last_updated = c['computed_at'] if last_updated is None else max(last_updated, c['computed_at'])

        refresh_button = ''
        if is_admin:
            token = new_csrf_token()
            refresh_button = f'''<form method="POST" action="/metrics/refresh" class="d-inline">
  <input type="hidden" name="csrf_token" value="{esc(token)}">
  <button type="submit" class="btn btn-primary btn-sm"><i class="bi bi-arrow-clockwise"></i> Refresh Metrics</button>
</form>'''

        if never_computed:
            body = f'''<div class="table-card"><div class="card-body text-center py-5">
  <i class="bi bi-graph-up-arrow" style="font-size:2.5rem;color:#94A3B8"></i>
  <h5 class="mt-3">No metrics calculated yet</h5>
  <p class="text-muted">{"Click Refresh Metrics to run the forecast, pricing, and anomaly models against your current data." if is_admin else "Ask an administrator to calculate metrics for this app."}</p>
  {refresh_button}
</div></div>'''
            content = f'''<div class="dash-greeting mb-4"><div><div class="dash-sub">Sales forecasting, pricing trends, and attendance anomaly detection.</div></div></div>
{alert}{body}'''
            return self._layout('Business Metrics', 'Business Metrics', content, 'metrics')

        sales_html = _forecast_series_html(forecast['sales'], 'salesForecastChart', '#DC2626', 'currency') if forecast else ''
        demand_html = _forecast_series_html(forecast['demand'], 'demandForecastChart', '#3B82F6', 'count') if forecast else ''
        pricing_html = _pricing_table_html(pricing) if pricing else '<div class="empty-state py-4"><p class="text-muted mb-0">Not yet calculated.</p></div>'
        anomalies_html = _anomalies_table_html(anomalies) if anomalies else '<div class="empty-state py-4"><p class="text-muted mb-0">Not yet calculated.</p></div>'

        content = f'''
<div class="dash-greeting mb-4">
  <div><div class="dash-sub">Sales forecasting, pricing trends, and attendance anomaly detection — last updated {esc(_when(last_updated))}.</div></div>
  <div>{refresh_button}</div>
</div>
{alert}
<div class="row g-4 mb-4">
  <div class="col-md-6">{sales_html}</div>
  <div class="col-md-6">{demand_html}</div>
</div>

<div class="row g-4">
  <div class="col-md-6">
    <div class="table-card h-100">
      <div class="card-header"><span><i class="bi bi-tags"></i> Pricing Suggestions</span></div>
      {pricing_html}
    </div>
  </div>
  <div class="col-md-6">
    <div class="table-card h-100">
      <div class="card-header"><span><i class="bi bi-shield-exclamation"></i> Attendance &amp; Salary Anomalies</span></div>
      {anomalies_html}
    </div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>'''
        return self._layout('Business Metrics', 'Business Metrics', content, 'metrics')

    # ── ACTION: recompute all three metrics (admin only) ────
    def handle_metrics_refresh(self, form, user_id):
        if not self._is_admin(user_id):
            return self.send(*redirect('/metrics'))
        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_metrics(user_id, err=True,
                                 msg='Your session expired — please try again.'),
                status=400))

        wait_left = seconds_since_last_refresh()
        if wait_left is not None and wait_left < MIN_REFRESH_INTERVAL_SECONDS:
            remaining = int(MIN_REFRESH_INTERVAL_SECONDS - wait_left)
            return self.send(*html_response(
                self.pg_metrics(user_id, err=True,
                                 msg=f'Please wait {remaining}s before refreshing again.'),
                status=429))

        now = time.time()
        try:
            forecast = compute_forecast()
            forecast['computed_at'] = now
            save_cache('forecast', forecast)

            pricing = compute_pricing_suggestions()
            pricing['computed_at'] = now
            save_cache('pricing', pricing)

            anomalies = compute_anomalies()
            anomalies['computed_at'] = now
            save_cache('anomalies', anomalies)
        except MLUnavailableError as e:
            return self.send(*html_response(
                self.pg_metrics(user_id, err=True, msg=str(e)), status=200))
        # Anything else (a genuine bug) is deliberately left to propagate:
        # do_POST's centralized handler (app/routing.py) logs it server-side
        # and shows a generic error, rather than this masking it as either
        # a "friendly" message or a silent partial success.

        return self.send(*redirect('/metrics'))
