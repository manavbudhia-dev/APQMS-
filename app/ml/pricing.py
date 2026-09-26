"""
Pricing suggestions.

Every quotation/proforma line that referenced a catalog item is a data
point of "what this item was actually priced at, and when". Rather than
fit one model per item (slow with many items, and each item's own
history is usually too short to trust on its own), this trains a
single shared model across every qualifying item at once: each item's
identity is one-hot encoded so the model can learn a per-item baseline
directly in its first layer, and a shared standardized month index
rides alongside it so the rest of the network learns one common time
trend on top of every item's baseline. This is the same idea recommender
systems use for per-entity effects (an embedding is, mechanically, a
learned per-category weight vector — a one-hot input into a dense layer
gets exactly that, just without the extra bottleneck an embedding table
would add), and it lets an item with only a handful of quotes still
borrow some signal from the overall pricing trend rather than being
ignored.

Items with fewer than MIN_POINTS_PER_ITEM historical rate entries are
left out of the suggestion table entirely — with that little history,
any number produced would be closer to a guess than a trend, and
showing it with the same confidence as a well-supported suggestion
would be misleading.
"""
from database import get_db
from utils.dates import date_to_iso
from app.ml.common import get_sklearn, MLUnavailableError

MIN_POINTS_PER_ITEM = 3
MIN_ITEMS_TO_TRAIN = 3       # below this, a shared model has nothing to share across
SIGNIFICANT_DELTA_PCT = 5.0  # only flag items whose suggestion differs this much
MAX_RESULTS = 50
MAX_ITER = 2000


def _collect_rate_history():
    """{item_id: [(month_index_float, rate), ...]} across both
    quotation_items and proforma_invoice_items, keyed to their parent
    document's date. month_index is months since the earliest observed
    point across the whole dataset, so it's comparable across items."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT qi.item_id, q.date, qi.rate FROM quotation_items qi "
            "JOIN quotations q ON q.id = qi.quotation_id "
            "WHERE qi.item_id IS NOT NULL AND qi.rate > 0 "
            "UNION ALL "
            "SELECT pii.item_id, p.date, pii.rate FROM proforma_invoice_items pii "
            "JOIN proforma_invoices p ON p.id = pii.proforma_id "
            "WHERE pii.item_id IS NOT NULL AND pii.rate > 0"
        ).fetchall()
        items = {r['id']: (r['item_name'], r['rate_per_unit'])
                 for r in db.execute("SELECT id, item_name, rate_per_unit FROM items")}
    finally:
        db.close()

    dated = []
    for r in rows:
        iso = date_to_iso(r['date'])
        if not iso:
            continue
        dated.append((r['item_id'], iso, float(r['rate'])))
    if not dated:
        return {}, items

    earliest = min(iso for _, iso, _ in dated)
    e_year, e_month = int(earliest[:4]), int(earliest[5:7])

    def month_index(iso):
        y, m = int(iso[:4]), int(iso[5:7])
        return (y - e_year) * 12 + (m - e_month)

    history = {}
    for item_id, iso, rate in dated:
        history.setdefault(item_id, []).append((float(month_index(iso)), rate))
    return history, items


def compute_pricing_suggestions():
    """Raises MLUnavailableError if scikit-learn isn't installed."""
    skl, np = get_sklearn()

    history, items = _collect_rate_history()
    qualifying = {iid: pts for iid, pts in history.items()
                  if len(pts) >= MIN_POINTS_PER_ITEM and iid in items}

    if len(qualifying) < MIN_ITEMS_TO_TRAIN:
        return {
            'status': 'insufficient_data',
            'note': f'Need at least {MIN_ITEMS_TO_TRAIN} items with '
                    f'{MIN_POINTS_PER_ITEM}+ historical quotes each to '
                    f'find a pricing trend (have {len(qualifying)}).',
            'suggestions': [],
            'items_considered': len(qualifying),
        }

    # Build training arrays: item index (0..N-1), month index, rate.
    item_ids = sorted(qualifying.keys())
    id_to_idx = {iid: i for i, iid in enumerate(item_ids)}
    n_items = len(item_ids)

    item_idx_col, month_col, rate_col = [], [], []
    for iid, pts in qualifying.items():
        for month, rate in pts:
            item_idx_col.append(id_to_idx[iid])
            month_col.append(month)
            rate_col.append(rate)

    month_arr = np.array(month_col, dtype='float64')
    rate_arr = np.array(rate_col, dtype='float64')
    month_mean, month_std = month_arr.mean(), max(month_arr.std(), 1e-9)
    rate_mean, rate_std = rate_arr.mean(), max(rate_arr.std(), 1e-9)
    month_std_arr = (month_arr - month_mean) / month_std
    rate_std_arr = (rate_arr - rate_mean) / rate_std

    # One-hot item identity (per-item baseline) concatenated with the
    # standardized month index (shared time trend) — see module
    # docstring for why this stands in for an embedding layer here.
    item_idx_arr = np.array(item_idx_col)
    onehot = np.zeros((len(item_idx_col), n_items))
    onehot[np.arange(len(item_idx_col)), item_idx_arr] = 1.0
    X = np.hstack([onehot, month_std_arr.reshape(-1, 1)])
    y = rate_std_arr

    try:
        model = skl.MLPRegressor(
            hidden_layer_sizes=(16, 8),
            activation='relu',
            solver='lbfgs',
            max_iter=MAX_ITER,
            random_state=0,
        )
        model.fit(X, y)

        # Predict "now" (one month past each item's latest observed point).
        latest_month = {iid: max(m for m, _ in pts) for iid, pts in qualifying.items()}
        pred_rows = []
        for iid in item_ids:
            row = [0.0] * n_items
            row[id_to_idx[iid]] = 1.0
            row.append((latest_month[iid] + 1 - month_mean) / month_std)
            pred_rows.append(row)
        pred_std = model.predict(np.array(pred_rows))
        predicted_rates = pred_std * rate_std + rate_mean
    except MLUnavailableError:
        raise
    except Exception:
        return {'status': 'model_error', 'suggestions': [],
                'note': 'Could not fit a pricing model to this data.',
                'items_considered': len(qualifying)}

    suggestions = []
    for iid, predicted in zip(item_ids, predicted_rates):
        item_name, catalog_rate = items[iid]
        catalog_rate = float(catalog_rate or 0)
        suggested = round(max(0.0, float(predicted)), 2)
        if catalog_rate > 0:
            delta_pct = round((suggested - catalog_rate) / catalog_rate * 100, 1)
        else:
            delta_pct = 0.0
        suggestions.append({
            'item_id': iid,
            'item_name': item_name,
            'catalog_rate': round(catalog_rate, 2),
            'suggested_rate': suggested,
            'delta_pct': delta_pct,
            'history_points': len(qualifying[iid]),
        })

    suggestions.sort(key=lambda s: abs(s['delta_pct']), reverse=True)
    notable = [s for s in suggestions if abs(s['delta_pct']) >= SIGNIFICANT_DELTA_PCT]

    return {
        'status': 'ok',
        'suggestions': suggestions[:MAX_RESULTS],
        'notable_count': len(notable),
        'items_considered': len(qualifying),
    }
