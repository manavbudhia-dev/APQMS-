"""
Sales & demand forecasting.

Two time series, both aggregated by calendar month:
  - "sales"  : confirmed order value from proforma invoices (quantity *
               rate on each line, pre-GST — the same base-value
               convention the dashboard already uses).
  - "demand" : count of new quotations raised that month (the pipeline
               of inquiries, independent of whether they converted).

A small MLP regressor is used *only* once there's enough history to
make that meaningful; with too few distinct months, fitting any model
(let alone a neural one) would just memorize noise, so this returns an
'insufficient_data' status and the caller shows the raw history without
pretending to forecast it.
"""
from datetime import datetime

from database import get_db
from utils.dates import date_to_iso
from app.ml.common import get_sklearn, standardize, MLUnavailableError

MIN_MONTHS_FOR_MODEL = 4
FORECAST_MONTHS_AHEAD = 3
MAX_ITER = 2000


def _monthly_series(rows_with_date_and_value):
    """rows_with_date_and_value: iterable of (date_str, value). Groups
    by YYYY-MM using the app's DD-MM-YYYY document date convention,
    skipping any date that doesn't parse (see date_to_iso — malformed
    hand-typed dates are excluded, not guessed at). Returns an
    ordered dict-like list of (month_key, total_value) sorted
    chronologically, with any gap months between the first and last
    filled in as zero so the trend isn't distorted by uneven spacing."""
    totals = {}
    for date_str, value in rows_with_date_and_value:
        iso = date_to_iso(date_str)
        if not iso:
            continue
        month_key = iso[:7]  # 'YYYY-MM'
        totals[month_key] = totals.get(month_key, 0.0) + value

    if not totals:
        return []

    months_sorted = sorted(totals.keys())
    first = datetime.strptime(months_sorted[0], '%Y-%m')
    last = datetime.strptime(months_sorted[-1], '%Y-%m')

    filled = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        key = f'{y:04d}-{m:02d}'
        filled.append((key, totals.get(key, 0.0)))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return filled


def _next_month_keys(last_month_key, n):
    y, m = int(last_month_key[:4]), int(last_month_key[5:7])
    out = []
    for _ in range(n):
        m += 1
        if m > 12:
            m = 1
            y += 1
        out.append(f'{y:04d}-{m:02d}')
    return out


def _fit_and_forecast(series, months_ahead):
    """series: [(month_key, value), ...], already gap-filled and sorted.
    Returns (forecast_values, trend) using a small MLPRegressor trained
    on standardized (month_index -> value) pairs. Kept deliberately
    tiny (two narrow hidden layers) because the input is at most a
    couple of dozen points — a large network would just overfit a
    series this short.

    Uses solver='lbfgs' rather than the default 'adam': lbfgs is a
    full-batch quasi-Newton optimizer scikit-learn itself recommends
    for small datasets, and it converges on a series this size in a
    fraction of a second with no minibatching/epoch bookkeeping to
    configure.
    """
    skl, np = get_sklearn()

    values = [v for _, v in series]
    x = list(range(len(values)))
    x_std, x_mean, x_scale = standardize(x)
    y_std, y_mean, y_scale = standardize(values)

    model = skl.MLPRegressor(
        hidden_layer_sizes=(16, 8),
        activation='relu',
        solver='lbfgs',
        max_iter=MAX_ITER,
        random_state=0,
    )
    X = np.array(x_std).reshape(-1, 1)
    y = np.array(y_std)
    model.fit(X, y)

    future_x = [len(values) + i for i in range(months_ahead)]
    future_x_std = (np.array(future_x) - x_mean) / x_scale
    pred_std = model.predict(future_x_std.reshape(-1, 1))
    forecast_values = [max(0.0, float(p * y_scale + y_mean)) for p in pred_std]

    if len(values) >= 2:
        recent_avg = sum(values[-min(3, len(values)):]) / min(3, len(values))
        forecast_avg = sum(forecast_values) / len(forecast_values)
        if forecast_avg > recent_avg * 1.08:
            trend = 'up'
        elif forecast_avg < recent_avg * 0.92:
            trend = 'down'
        else:
            trend = 'flat'
    else:
        trend = 'flat'

    return forecast_values, trend


def _build_result(series, label, unit):
    if not series:
        return {'label': label, 'unit': unit, 'status': 'no_data',
                'history': [], 'forecast': []}

    history = [{'month': k, 'value': round(v, 2)} for k, v in series]

    if len(series) < MIN_MONTHS_FOR_MODEL:
        return {'label': label, 'unit': unit, 'status': 'insufficient_data',
                'history': history, 'forecast': [],
                'note': f'Need at least {MIN_MONTHS_FOR_MODEL} months of '
                        f'history to forecast (have {len(series)}).'}

    try:
        forecast_values, trend = _fit_and_forecast(series, FORECAST_MONTHS_AHEAD)
    except MLUnavailableError:
        raise
    except Exception:
        # A model that fails to converge on an unusual series (e.g. a
        # single huge outlier month) should degrade to "no forecast",
        # not take the whole page down.
        return {'label': label, 'unit': unit, 'status': 'model_error',
                'history': history, 'forecast': [],
                'note': 'Could not fit a forecast to this data.'}

    future_months = _next_month_keys(series[-1][0], FORECAST_MONTHS_AHEAD)
    forecast = [{'month': m, 'value': round(v, 2)}
                for m, v in zip(future_months, forecast_values)]

    return {'label': label, 'unit': unit, 'status': 'ok',
            'history': history, 'forecast': forecast, 'trend': trend}


def compute_forecast():
    """Top-level entry point used by the Business Metrics page. Raises
    MLUnavailableError if scikit-learn isn't installed — callers should
    catch that and show the install-hint message."""
    get_sklearn()  # fail fast, before touching the DB, if sklearn is unavailable

    db = get_db()
    try:
        sales_rows = db.execute(
            "SELECT p.date, pii.quantity, pii.rate FROM proforma_invoice_items pii "
            "JOIN proforma_invoices p ON p.id = pii.proforma_id"
        ).fetchall()
        sales_series = _monthly_series(
            (r['date'], (r['quantity'] or 0) * (r['rate'] or 0)) for r in sales_rows
        )

        demand_rows = db.execute("SELECT date FROM quotations").fetchall()
        demand_series = _monthly_series((r['date'], 1) for r in demand_rows)
    finally:
        db.close()

    return {
        'sales': _build_result(sales_series, 'Sales (confirmed orders)', 'INR'),
        'demand': _build_result(demand_series, 'Demand (new quotations)', 'count'),
    }
