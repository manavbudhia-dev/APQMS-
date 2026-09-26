"""
Attendance & salary anomaly detection.

Builds one feature row per (employee, month): attendance rate, total
overtime hours, and advance amount as a fraction of basic salary. With
enough rows across employees/months, a small autoencoder — an
MLPRegressor trained to reconstruct its own input through a
2-dimensional bottleneck — is fit to that feature matrix; rows it
reconstructs poorly are the ones that don't fit the general pattern
the rest of the data has, which is what "anomalous" means here: not a
judgement about the employee, just a flag that this particular
month's numbers stand out and may be worth a look.

With too little data for that to be meaningful, this falls back to a
plain per-feature z-score check instead of hiding the feature or
forcing a neural net onto a handful of points — the fallback is
clearly labeled as such wherever it's shown, rather than presented
with the same confidence as the trained-model result.
"""
from datetime import datetime
import calendar

from database import get_db
from app.ml.common import get_sklearn, MLUnavailableError

MIN_ROWS_FOR_AUTOENCODER = 8
RECONSTRUCTION_ERROR_THRESHOLD_STD = 2.0
ZSCORE_FALLBACK_THRESHOLD = 2.5
MAX_ITER = 2000


def _monthly_employee_features():
    """One row per (employee, month) that has at least one attendance
    entry: [days_present, overtime_hours, advance_ratio]."""
    db = get_db()
    try:
        employees = db.execute(
            "SELECT id, name, basic_salary, advance FROM employees"
        ).fetchall()
        att_rows = db.execute(
            "SELECT employee_id, date, is_present, overtime_hours FROM attendance"
        ).fetchall()
    finally:
        db.close()

    emp_by_id = {e['id']: e for e in employees}
    buckets = {}  # (employee_id, 'YYYY-MM') -> {'present': n, 'ot': hours}
    for r in att_rows:
        date = r['date'] or ''
        if len(date) < 7:
            continue
        month_key = date[:7]
        key = (r['employee_id'], month_key)
        b = buckets.setdefault(key, {'present': 0, 'ot': 0.0})
        if r['is_present']:
            b['present'] += 1
        b['ot'] += float(r['overtime_hours'] or 0)

    rows = []
    for (emp_id, month_key), b in buckets.items():
        emp = emp_by_id.get(emp_id)
        if emp is None:
            continue
        year, month = int(month_key[:4]), int(month_key[5:7])
        days_in_month = calendar.monthrange(year, month)[1]
        attendance_rate = b['present'] / days_in_month
        basic_salary = float(emp['basic_salary'] or 0)
        advance_ratio = (float(emp['advance'] or 0) / basic_salary) if basic_salary > 0 else 0.0
        rows.append({
            'employee_id': emp_id,
            'employee_name': emp['name'],
            'month': month_key,
            'features': [attendance_rate, b['ot'], advance_ratio],
            'raw': {'days_present': b['present'], 'overtime_hours': round(b['ot'], 1),
                    'advance_ratio': round(advance_ratio, 3)},
        })
    return rows


def _zscore_fallback(rows):
    if not rows:
        return []
    n_features = len(rows[0]['features'])
    means, stds = [], []
    for f in range(n_features):
        vals = [r['features'][f] for r in rows]
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        means.append(m)
        stds.append(var ** 0.5 or 1.0)

    feature_names = ['attendance rate', 'overtime hours', 'advance-to-salary ratio']
    flagged = []
    for r in rows:
        reasons = []
        max_z = 0.0
        for f in range(n_features):
            z = (r['features'][f] - means[f]) / stds[f]
            max_z = max(max_z, abs(z))
            if abs(z) >= ZSCORE_FALLBACK_THRESHOLD:
                direction = 'unusually high' if z > 0 else 'unusually low'
                reasons.append(f'{feature_names[f]} {direction}')
        if reasons:
            flagged.append({
                'employee_id': r['employee_id'], 'employee_name': r['employee_name'],
                'month': r['month'], 'reasons': reasons,
                'severity': round(max_z, 2), 'raw': r['raw'], 'method': 'statistical',
            })
    flagged.sort(key=lambda x: x['severity'], reverse=True)
    return flagged


def compute_anomalies():
    """Raises MLUnavailableError if scikit-learn isn't installed."""
    skl, np = get_sklearn()

    rows = _monthly_employee_features()
    if len(rows) < 3:
        return {'status': 'insufficient_data', 'anomalies': [],
                'note': 'Not enough attendance history yet to check for anomalies.',
                'rows_considered': len(rows)}

    if len(rows) < MIN_ROWS_FOR_AUTOENCODER:
        flagged = _zscore_fallback(rows)
        return {'status': 'ok', 'method': 'statistical', 'anomalies': flagged,
                'rows_considered': len(rows),
                'note': f'Using a statistical check (need {MIN_ROWS_FOR_AUTOENCODER}+ '
                        f'employee-months for the neural model; have {len(rows)}).'}

    feature_matrix = np.array([r['features'] for r in rows], dtype='float64')
    means = feature_matrix.mean(axis=0)
    stds = feature_matrix.std(axis=0)
    stds[stds < 1e-9] = 1.0
    standardized = (feature_matrix - means) / stds
    n_features = feature_matrix.shape[1]

    try:
        # Dense(4) -> Dense(2) bottleneck -> Dense(4) -> output, trained
        # to reproduce its own input: scikit-learn has no dedicated
        # autoencoder class, but MLPRegressor supports multi-output
        # regression, so fitting it on (X, X) with a narrow middle
        # layer gives the same reconstruction-error signal.
        #
        # alpha (L2 regularization) is turned up well past scikit-learn's
        # default here on purpose: the default is tuned for datasets far
        # bigger than the handful of employee-months this trains on, and
        # at that default strength a network this size, trained with
        # lbfgs to convergence, has enough free capacity to special-case
        # and perfectly reconstruct even a genuinely anomalous row rather
        # than being forced to generalize across the normal ones — which
        # defeats the entire reconstruction-error premise. The stronger
        # penalty constrains the network enough that only the common
        # pattern gets reconstructed well, which is what actually makes
        # an outlier's error stand out; verified empirically against
        # realistic-scale attendance data (see SECURITY_CHANGES.md).
        model = skl.MLPRegressor(
            hidden_layer_sizes=(4, 2, 4),
            activation='relu',
            solver='lbfgs',
            alpha=3.0,
            max_iter=MAX_ITER,
            random_state=0,
        )
        model.fit(standardized, standardized)
        reconstructed = model.predict(standardized)
        errors = np.mean((standardized - reconstructed) ** 2, axis=1)
    except MLUnavailableError:
        raise
    except Exception:
        flagged = _zscore_fallback(rows)
        return {'status': 'ok', 'method': 'statistical', 'anomalies': flagged,
                'rows_considered': len(rows),
                'note': 'Neural model failed to train; showing a statistical check instead.'}

    err_mean, err_std = float(errors.mean()), float(errors.std() or 1.0)
    threshold = err_mean + RECONSTRUCTION_ERROR_THRESHOLD_STD * err_std

    feature_names = ['attendance rate', 'overtime hours', 'advance-to-salary ratio']
    flagged = []
    for r, err, std_row in zip(rows, errors, standardized):
        if err <= threshold:
            continue
        # Which feature(s) drove the reconstruction error, for a human-readable reason.
        reasons = [feature_names[i] for i in range(n_features) if abs(std_row[i]) >= 1.5]
        if not reasons:
            reasons = ['overall pattern']
        flagged.append({
            'employee_id': r['employee_id'], 'employee_name': r['employee_name'],
            'month': r['month'],
            'reasons': [f'{name} out of line with usual pattern' for name in reasons],
            'severity': round(float((err - err_mean) / err_std), 2),
            'raw': r['raw'], 'method': 'neural',
        })
    flagged.sort(key=lambda x: x['severity'], reverse=True)

    return {'status': 'ok', 'method': 'neural', 'anomalies': flagged,
            'rows_considered': len(rows)}
