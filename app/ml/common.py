"""
Shared plumbing for the Business Metrics module (app/ml/*).

scikit-learn is an optional dependency (see requirements-ml.txt) — the
rest of this app (quotations, proforma invoices, packing, salary) must
keep working whether or not it's installed. Every entry point here
funnels through get_sklearn(), which imports lazily and raises a
single, recognisable MLUnavailableError the view layer can catch and
turn into a friendly "install this to enable the feature" message
instead of a 500. Nothing in this app trains or loads a model except
code we wrote ourselves in this package — this is not a path that ever
deserializes a model or data file supplied by a user or another server,
so the usual concerns about untrusted pickle/model files don't apply
here. Results are still only ever cached as plain JSON (never pickled)
to keep it that way even as this module evolves.

scikit-learn (plus its own numpy/scipy dependencies) is a small, pure
wheel-installable package on every platform this app targets, unlike
the TensorFlow build it replaces — that matters here because this
app's other deployment target is shared/cPanel hosting via
passenger_wsgi.py (see README.md), where a ~250-300MB dependency with
its own Python-version/OS/CPU wheel requirements was often simply not
installable.
"""
import json
import os
import time
import types

from app.config import BASE
from utils.http_utils import _atomic_write_private

CACHE_DIR = os.path.join(BASE, 'app', 'ml', '_cache')
MIN_REFRESH_INTERVAL_SECONDS = 30  # basic anti-hammering guard on the
                                    # admin-triggered retrain endpoint
_REFRESH_SLOT_FILE = os.path.join(CACHE_DIR, 'refresh.lock')
REFRESH_SLOT_STALE_SECONDS = 10 * 60  # a retrain never legitimately takes
                                      # this long, so the slot is reclaimable


class MLUnavailableError(RuntimeError):
    """Raised when scikit-learn/NumPy aren't installed. Callers show a
    friendly message rather than letting this become a 500."""
    pass


_skl_cache = None
_np_cache = None


def get_sklearn():
    """Import scikit-learn + NumPy on first use only, and only from
    this one place, so every model module shares one cached import
    rather than each re-importing, and so an ImportError surfaces as
    one consistent, catchable error type everywhere it's used.

    Returns (skl, np) where `skl` is a small namespace exposing just
    the estimator classes app/ml/* actually uses (scikit-learn doesn't
    guarantee its submodules are attached to the top-level package the
    way `tf.keras` is, so those submodules are imported explicitly
    here rather than assumed)."""
    global _skl_cache, _np_cache
    if _skl_cache is not None:
        return _skl_cache, _np_cache
    try:
        import numpy as np
        from sklearn.neural_network import MLPRegressor
        from sklearn.preprocessing import StandardScaler
    except ImportError as e:
        raise MLUnavailableError(
            "scikit-learn is not installed on this server. Install the "
            "optional ML dependencies (see requirements-ml.txt) to "
            "enable Business Metrics."
        ) from e
    skl = types.SimpleNamespace(
        MLPRegressor=MLPRegressor,
        StandardScaler=StandardScaler,
    )
    _skl_cache, _np_cache = skl, np
    return skl, np


def _cache_path(name):
    # `name` is always one of a small fixed set of literal strings this
    # module defines itself (never request-derived), so there's no
    # path-traversal surface here — but keep it to a safe slug anyway.
    safe = ''.join(c for c in name if c.isalnum() or c in ('-', '_'))
    return os.path.join(CACHE_DIR, f'{safe}.json')


def load_cache(name):
    """Returns the cached dict for `name`, or None if there isn't one
    yet / it's unreadable. Never raises."""
    path = _cache_path(name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cache(name, data):
    """Atomic, owner-only-permission write — same pattern as the
    session/CSRF files in utils/http_utils.py, since this cache could
    in principle mention business figures (revenue, salary anomalies)
    that shouldn't be left world-readable on shared hosting either."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    _atomic_write_private(_cache_path(name), lambda f: json.dump(data, f))


def seconds_since_last_refresh():
    """None if never refreshed; otherwise seconds since the most recent
    of the three metric caches was written."""
    latest = None
    for name in ('forecast', 'pricing', 'anomalies'):
        cached = load_cache(name)
        if cached and 'computed_at' in cached:
            latest = cached['computed_at'] if latest is None else max(latest, cached['computed_at'])
    if latest is None:
        return None
    return time.time() - latest


def claim_refresh_slot():
    """Take the "one retrain at a time" slot, or say it's already taken.

    `seconds_since_last_refresh()` is only a rate limit, and it was checked
    against the *previous* run's timestamp: two admins pressing Refresh
    together both saw "more than 30s ago" and both then trained and wrote
    the same three caches. Creating this file with O_EXCL is the one
    primitive that makes "is anyone else inside the critical section right
    now?" a single atomic question, across processes as well as threads.

    A slot left behind by a process that died mid-train would block
    refreshing forever, so one older than REFRESH_SLOT_STALE_SECONDS is
    reclaimed rather than honoured."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    for attempt in (0, 1):
        try:
            fd = os.open(_REFRESH_SLOT_FILE,
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if attempt:
                return False
            try:
                if (time.time() - os.path.getmtime(_REFRESH_SLOT_FILE)
                        > REFRESH_SLOT_STALE_SECONDS):
                    os.remove(_REFRESH_SLOT_FILE)
            except OSError:
                pass
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    return False


def release_refresh_slot():
    try:
        os.remove(_REFRESH_SLOT_FILE)
    except OSError:
        pass


def sklearn_available():
    """Cheap check for the view layer: is scikit-learn importable at
    all? Unlike get_sklearn(), never raises — used to decide what to
    render (the real feature vs. an install hint), not to gate the
    training code itself."""
    try:
        get_sklearn()
        return True
    except MLUnavailableError:
        return False


def standardize(values):
    """Plain mean/std standardization (z-scores), done with plain numpy
    rather than reaching for a scikit-learn Pipeline just for this.
    Returns (standardized_list, mean, std) with std floored at a small
    epsilon so a constant series never divides by zero."""
    _, np = get_sklearn()
    arr = np.array(values, dtype='float64')
    mean = float(arr.mean()) if len(arr) else 0.0
    std = float(arr.std()) if len(arr) else 1.0
    if std < 1e-9:
        std = 1.0
    return ((arr - mean) / std), mean, std
