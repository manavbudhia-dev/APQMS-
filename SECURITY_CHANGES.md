# Security & safety guardrails applied — APQMS

Audit of the whole project (a hand-rolled Python `http.server` app — no
Flask/Django). The codebase already had solid fundamentals (parameterized
SQL everywhere, PBKDF2 password hashing with timing-safe comparison,
login lockout, HttpOnly/SameSite cookies, admin-gated backup routes,
path-traversal-safe static file serving, open-redirect protection on the
`next=` param). The gaps below were found and fixed; all changes were
smoke-tested against a running instance (login, CSRF rejection, XSS
escaping, PDF generation with hostile input, and a full page-by-page
regression pass all pass — see test transcript in this conversation).

## Fixed

1. **CSRF coverage gap** (`app/routing.py`, `utils/http_utils.py`)
   Only 7 of ~44 state-changing POST routes carried a per-form CSRF
   token (login/register/change-password + a few settings routes).
   Everything else — customer/item/category/quotation/proforma/
   packing/salary CRUD, bulk delete, user activate/delete — had none.
   Added `is_same_origin()`, checked once centrally in `do_POST` for
   *every* POST request: rejects (403) when `Origin` (or, failing that,
   `Referer`) names a different site than this one. This is on top of,
   not instead of, the existing per-form tokens and the session
   cookie's `SameSite=Lax`. Verified: forged `Origin` → 403; legitimate
   same-origin request → succeeds; existing token checks still enforced
   independently.

2. **Stored XSS via page title/heading** (`app/models/base_mixin.py`)
   `_layout()` inserted its `title`/`heading` arguments — frequently
   built from user-editable data (category name, customer company
   name, employee name) — into `<title>` and the page `<h5>` heading
   without escaping. Any logged-in user could plant a `<script>` tag in
   one of those fields and have it execute in whoever next viewed that
   record, including an admin. Fixed by escaping both centrally inside
   `_layout()`, so every one of its ~25 callers is covered at once.

3. **PDF markup injection** (`app/generators/pdf_generator.py`,
   `proforma_generator.py`, `packing_generator.py`)
   ReportLab's `Paragraph` interprets its text as a small XML/HTML
   dialect. Several user-editable fields (customer name, quotation
   subject, item specification, unit, terms, order no./date) were
   interpolated into `Paragraph()` calls unescaped, while sibling
   fields (address, item name/description, notes) already went through
   the existing `_rl_multiline()`/`_format_notes()` escapers. Applied
   the same escaping to the missed fields. Confirmed via a real
   ReportLab test that plain `Table` cell strings (as opposed to
   `Paragraph` objects) are not markup-parsed, so only genuine
   `Paragraph()` call sites needed the change. Verified: a payload of
   `<script>`, `<img src=x>`, `<b>`, `&` across every affected field no
   longer breaks PDF generation and now renders as literal, safe text.

4. **Stack-trace disclosure** (`passenger_wsgi.py`, `app/routing.py`)
   The WSGI adapter's exception handler returned the full Python
   traceback — file paths, request internals — directly in the HTTP
   response body. Now logged server-side only (`traceback.print_exc`),
   with a generic message sent to the client. Added the same
   catch-log-generic-500 wrapper around `do_GET`/`do_POST` in
   `app/routing.py` so this holds regardless of which entry point
   (standalone `server.py` or `passenger_wsgi.py`) is running, and
   regardless of which handler raises. Verified with both a live
   trigger and a direct unit check: client sees a generic 500 page,
   server log gets the full traceback.

5. **Missing security response headers** (`utils/http_utils.py`,
   `app/models/base_mixin.py`)
   Added `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
   `Referrer-Policy: same-origin`, a restrictive `Permissions-Policy`,
   a `Content-Security-Policy` (self + the two CDNs already in use;
   `object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`),
   and `Cache-Control: no-store` on app responses (static assets stay
   cacheable). `'unsafe-inline'` is kept for script/style — the UI
   relies throughout on inline `<script>`/`onclick=`, and removing that
   is a front-end rewrite of its own; the actual XSS defense is item 2
   above, not CSP.

6. **World-readable secret files** (`utils/http_utils.py`)
   `sessions.json`, `csrf_tokens.json`, and `login_attempts.json` were
   written with default (world-readable, `0644`) permissions on disk —
   on shared hosting, another local account could read live session
   IDs. Now written `0600`, consistent with how `ADMIN_CREDENTIALS.txt`
   was already handled.

7. **Housekeeping**
   - `requirements.txt`: pinned `reportlab` to `>=4.2,<5` instead of an
     unbounded version.
   - Added `.gitignore` for the database, generated admin credentials,
     server-side backups, and the session/CSRF/login-attempt JSON files
     — none of that belongs in version control.

## Business Metrics module (added after the initial audit)

A new `/metrics` page (TensorFlow-backed sales/demand forecasting,
pricing trend suggestions, and attendance/salary anomaly detection) was
added afterward. It follows the same guardrails as the rest of the app
rather than being a special case:

- **Auth & CSRF**: `/metrics` and `/metrics/refresh` are ordinary routes
  behind the existing session-required dispatch in `app/routing.py`, so
  they automatically get the centralized Origin/Referer CSRF check
  (item 1 above). The refresh form additionally carries its own
  per-form CSRF token, matching the pattern used by the other
  admin-only actions (backup/restore/user management).
- **Authorization**: any logged-in user can view the page (like the
  Dashboard); only an admin (`_is_admin`) can trigger a recompute,
  since training — however small here — is real, deliberate CPU work,
  unlike a normal form save.
- **Anti-abuse**: a 30-second minimum interval between refreshes,
  independent of the CSRF/auth checks, so the admin-gated action can't
  be hammered even by an admin's own script.
- **No pickle, ever**: model results are cached as plain JSON only
  (`app/ml/common.py`); no model object is ever serialized to or
  deserialized from disk. Nothing in this module reads a model or data
  file supplied by a user or another server.
- **XSS**: item and employee names (user-editable data) reach the page
  only through the existing `esc()` helper in the HTML tables; the
  only values embedded into inline `<script>` blocks (for the Chart.js
  forecast charts) are numeric series and `YYYY-MM` month keys — never
  free-text fields — so there's no path for user data to reach a
  `<script>` context unescaped.
- **Optional dependency, isolated failure**: TensorFlow lives in
  `requirements-ml.txt`, separate from `requirements.txt`, and every
  entry point funnels through one lazy import
  (`app/ml/common.get_tf()`). If it isn't installed, `/metrics` shows
  an install hint and every other page is unaffected — this was a
  deliberate design constraint given the app's other deployment target
  is shared/cPanel hosting (`passenger_wsgi.py`), where a 250-300 MB
  dependency may not be installable at all.
- **Performance note, not a security issue, but worth recording**:
  initial versions used Keras's `model.fit()`, which took ~40+ seconds
  per model on tiny datasets due to per-epoch bookkeeping overhead —
  long enough to be a poor-but-not-dangerous user experience on an
  admin-triggered action. Rewritten to a manual `@tf.function` +
  `GradientTape` training step, bringing a full three-model refresh
  under a live dataset to ~2-3 seconds in testing.

## ML backend swap: TensorFlow → scikit-learn (added after the above)

The Business Metrics module described above originally trained its three
models on TensorFlow/Keras. It has since been re-implemented on
scikit-learn (`app/ml/common.get_sklearn()`, `MLPRegressor`), for the same
reason the "optional dependency, isolated failure" bullet above already
called out: TensorFlow's ~250-300 MB, platform-specific wheel was a poor
fit for the shared/cPanel hosting target, and often not installable there
at all. scikit-learn has no such constraint. The three bullets above that
described TensorFlow-specific mechanics are superseded as follows; every
other guardrail (auth, CSRF, anti-abuse, no pickle, XSS-safe rendering)
is unchanged and re-verified against the new code:

- **Optional dependency, isolated failure**: unchanged in shape — the
  dependency now lives as `scikit-learn` in `requirements-ml.txt`, and
  every entry point still funnels through one lazy import
  (`app/ml/common.get_sklearn()`), raising the same `MLUnavailableError`
  the view layer already knew how to catch.
- **Performance**: scikit-learn's `MLPRegressor` with `solver='lbfgs'`
  (a full-batch optimizer scikit-learn itself recommends for small
  datasets) fits each of the three tiny models in well under a second,
  with no manual training-loop code needed to avoid per-epoch overhead.
- **Modeling approach, same shape as before**: forecasting and pricing
  still standardize inputs and fit a small MLP; pricing's TensorFlow
  embedding layer is replaced with a one-hot item-identity input (a
  one-hot vector into a dense layer learns the same kind of per-item
  baseline an embedding table would, without the extra bottleneck
  dimension); the anomaly detector's autoencoder is now an `MLPRegressor`
  fit to reconstruct its own standardized input through a `(4, 2, 4)`
  bottleneck — mechanically the same network TensorFlow was training
  before, just expressed as one estimator instead of a hand-written
  Keras model + gradient-tape loop.

## Noted, not changed

- `utils/backup_crypto.py` uses a custom PBKDF2 + SHAKE-256-keystream +
  HMAC-SHA256 encrypt-then-MAC construction (stdlib-only, by design, per
  its own docstring). It's sound (fresh random nonce per file,
  constant-time tag comparison) but non-standard; swapping in AES-GCM
  via the `cryptography` package would be strictly better if that
  dependency ever becomes acceptable to add.
- A number of `int()`/`float()` conversions on form input elsewhere in
  the view layer are already wrapped in their own `try/except`, but not
  all were individually re-audited; the new generic 500 handler (item 4)
  means any that aren't will fail safely (no leaked internals) even so.
