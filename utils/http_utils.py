"""
Shared, dependency-free helpers used across all page/handler modules:
HTML escaping, currency formatting, the in-memory session store, request
form parsing, small response-tuple builders (body, status, headers), and
basic auth-security helpers (CSRF tokens, login rate limiting).
"""
import json
import os
import re
import secrets
import time
import urllib.parse
import html as _html
from http.cookies import SimpleCookie, CookieError
from threading import Lock


def _atomic_write_private(path, write_fn):
    """Write a file atomically (tmp + os.replace) with owner-only (0600)
    permissions, since every file this is used for holds bearer-equivalent
    secrets (session ids, CSRF tokens, failed-login counters keyed by
    username). Default file permissions (0644) would leave them readable
    by any other local account on a shared host — set the mode on the
    temp file *before* any content is written, and before the atomic
    rename makes it visible, so there's no window where the data sits
    world-readable."""
    tmp_path = path + '.tmp'
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            write_fn(f)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)  # atomic on POSIX


def esc(s):
    """HTML-escape a value for safe embedding in markup/attributes."""
    return _html.escape(str(s), quote=True)


def esc_nl(s):
    """HTML-escape a value and turn newlines into <br> for read-only display
    of multi-line fields (e.g. a customer's address entered line by line)."""
    return esc(s).replace('\n', '<br>')


def fmt_rupee(n):
    """Format a number with Indian digit grouping (lakh/crore), e.g.
    1234567.5 -> "12,34,567.50". Python's ',' format spec groups every 3
    digits (Western "million" style: 12,34,567 would come out as
    1,234,567), so the grouping has to be done by hand; paise (the
    fractional part) are kept rather than rounded away with `.0f`."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "0"
    sign = '-' if value < 0 else ''
    value = abs(value)
    rupees = int(value)
    paise = round((value - rupees) * 100)
    if paise == 100:  # rounding pushed the paise over to the next rupee
        rupees += 1
        paise = 0

    digits = str(rupees)
    if len(digits) > 3:
        last3, rest = digits[-3:], digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ','.join(groups) + ',' + last3
    else:
        grouped = digits

    return f"{sign}{grouped}.{paise:02d}"


# Set COOKIE_SECURE=1 in the environment once the site is served over
# HTTPS (which it should be, on a public server) so session cookies get
# the "Secure" flag and are never sent in the clear.
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '0') == '1'

# Server-side session lifetime, enforced independently of the cookie's
# Max-Age. This bounds how long a stolen/lingering session stays valid.
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60  # 7 days


# ── SESSION STORE (persisted to disk) ──
# Kept as a JSON file next to this module so sessions survive process
# restarts (dev-server auto-reload) and are shared across worker processes
# (gunicorn/uwsgi with multiple workers), instead of living only in a
# per-process dict that gets wiped on every restart/new worker.
_SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'sessions.json')
_lock = Lock()


def _load_sessions():
    if not os.path.exists(_SESSIONS_FILE):
        return {}
    try:
        with open(_SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(sessions):
    _atomic_write_private(_SESSIONS_FILE, lambda f: json.dump(sessions, f))


def get_session(cookie_header):
    if not cookie_header:
        return {}
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except CookieError:
        return {}
    sid = c.get('sid')
    if not sid:
        return {}
    with _lock:
        sessions = _load_sessions()
        data = sessions.get(sid.value)
        if not data:
            return {}
        # Enforce session lifetime server-side, independent of the cookie.
        if time.time() - data.get('_created', 0) > SESSION_LIFETIME_SECONDS:
            sessions.pop(sid.value, None)
            _save_sessions(sessions)
            return {}
    return data


def create_session(user_data):
    sid = secrets.token_hex(32)  # 256 bits of entropy
    data = dict(user_data)
    data['_created'] = time.time()
    with _lock:
        sessions = _load_sessions()
        sessions[sid] = data
        _save_sessions(sessions)
    return sid


def update_session(cookie_header, **fields):
    """Patch fields onto the caller's current session in place (e.g. after
    a profile edit changes full_name), so the sidebar/topbar reflect the
    change immediately instead of waiting for the next login."""
    if not cookie_header:
        return
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except CookieError:
        return
    sid = c.get('sid')
    if not sid:
        return
    with _lock:
        sessions = _load_sessions()
        data = sessions.get(sid.value)
        if data is None:
            return
        data.update(fields)
        sessions[sid.value] = data
        _save_sessions(sessions)


def destroy_session(cookie_header):
    """Remove a session, e.g. on explicit logout."""
    if not cookie_header:
        return
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except CookieError:
        return
    sid = c.get('sid')
    if not sid:
        return
    with _lock:
        sessions = _load_sessions()
        sessions.pop(sid.value, None)
        _save_sessions(sessions)


class _SessionsProxy:
    """Dict-like view onto the on-disk session store, kept so code
    elsewhere in the app that does `from utils.http_utils import
    sessions` and looks things up directly (e.g. `sid in sessions`,
    `sessions[sid]`) keeps working without change."""

    def __contains__(self, sid):
        with _lock:
            return sid in _load_sessions()

    def __getitem__(self, sid):
        with _lock:
            return _load_sessions()[sid]

    def __setitem__(self, sid, value):
        with _lock:
            sessions = _load_sessions()
            sessions[sid] = value
            _save_sessions(sessions)

    def __delitem__(self, sid):
        with _lock:
            sessions = _load_sessions()
            sessions.pop(sid, None)
            _save_sessions(sessions)

    def __len__(self):
        with _lock:
            return len(_load_sessions())

    def __iter__(self):
        with _lock:
            return iter(_load_sessions())

    def get(self, sid, default=None):
        with _lock:
            return _load_sessions().get(sid, default)

    def pop(self, sid, default=None):
        with _lock:
            sessions = _load_sessions()
            value = sessions.pop(sid, default)
            _save_sessions(sessions)
            return value

    def items(self):
        with _lock:
            return list(_load_sessions().items())

    def keys(self):
        with _lock:
            return list(_load_sessions().keys())

    def values(self):
        with _lock:
            return list(_load_sessions().values())


# Backward-compatible direct handle onto the session store — prefer
# get_session()/create_session()/destroy_session() in new code, but this
# keeps existing `from utils.http_utils import sessions` call sites working.
sessions = _SessionsProxy()


def session_cookie_header(sid, max_age=604800):
    """Build a Set-Cookie value for a login session with sane security
    flags (HttpOnly always; Secure when COOKIE_SECURE=1)."""
    value = f'sid={sid}; Path=/; HttpOnly; Max-Age={max_age}; SameSite=Lax'
    if COOKIE_SECURE:
        value += '; Secure'
    return value


def logout_cookie_header():
    """Set-Cookie value that clears the session cookie client-side."""
    value = 'sid=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax'
    if COOKIE_SECURE:
        value += '; Secure'
    return value


# ── CSRF PROTECTION ──
# Single-use tokens, independent of the session cookie, so they work on
# the login/register pages before a session even exists. A token is
# stamped into the form when the page is rendered and consumed (checked
# + deleted) the moment it's submitted, which is enough to stop a
# cross-site form submission: an attacker's page cannot read the token
# out of a response it isn't allowed to see.
_CSRF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'csrf_tokens.json')
_csrf_lock = Lock()
CSRF_TOKEN_LIFETIME = 60 * 60  # 1 hour


def _load_csrf():
    if not os.path.exists(_CSRF_FILE):
        return {}
    try:
        with open(_CSRF_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_csrf(tokens):
    _atomic_write_private(_CSRF_FILE, lambda f: json.dump(tokens, f))


def new_csrf_token():
    """Create a token to embed as a hidden field in an outgoing form."""
    token = secrets.token_hex(24)
    now = time.time()
    with _csrf_lock:
        tokens = _load_csrf()
        tokens = {t: exp for t, exp in tokens.items() if exp > now}  # sweep
        tokens[token] = now + CSRF_TOKEN_LIFETIME
        _save_csrf(tokens)
    return token


def consume_csrf_token(token):
    """Validate and invalidate a submitted CSRF token (single use)."""
    if not token:
        return False
    with _csrf_lock:
        tokens = _load_csrf()
        exp = tokens.pop(token, None)
        _save_csrf(tokens)
    return bool(exp) and exp > time.time()


# ── LOGIN RATE LIMITING / ACCOUNT LOCKOUT ──
# Tracks failed attempts per (username, IP) pair so a brute-force script
# gets locked out without permanently locking a user out from every
# network, and without needing a DB migration.
_ATTEMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'login_attempts.json')
_attempts_lock = Lock()

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes


def _load_attempts():
    if not os.path.exists(_ATTEMPTS_FILE):
        return {}
    try:
        with open(_ATTEMPTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_attempts(attempts):
    _atomic_write_private(_ATTEMPTS_FILE, lambda f: json.dump(attempts, f))


def _attempt_key(identifier, ip):
    return f"{(identifier or '').lower()}|{ip or ''}"


def is_locked_out(identifier, ip):
    """Return seconds remaining if locked out, or 0 if free to try."""
    key = _attempt_key(identifier, ip)
    with _attempts_lock:
        attempts = _load_attempts()
        entry = attempts.get(key)
        if not entry:
            return 0
        if entry['count'] >= MAX_LOGIN_ATTEMPTS:
            remaining = LOCKOUT_SECONDS - (time.time() - entry['last'])
            if remaining > 0:
                return int(remaining)
            attempts.pop(key, None)
            _save_attempts(attempts)
    return 0


def record_failed_login(identifier, ip):
    key = _attempt_key(identifier, ip)
    now = time.time()
    with _attempts_lock:
        attempts = _load_attempts()
        # Sweep every entry whose lockout window has fully elapsed since
        # its last failure — same idea as new_csrf_token()'s sweep. This
        # is what makes old typos decay (rather than accumulating toward
        # MAX_LOGIN_ATTEMPTS forever) and keeps the file from growing
        # without bound; it also means an entry can't cause an instant
        # re-lockout just because is_locked_out() happened not to be
        # called first to purge it.
        attempts = {k: e for k, e in attempts.items()
                    if now - e.get('last', 0) < LOCKOUT_SECONDS}
        entry = attempts.get(key, {'count': 0, 'last': 0})
        entry['count'] += 1
        entry['last'] = now
        attempts[key] = entry
        _save_attempts(attempts)


def clear_failed_logins(identifier, ip):
    key = _attempt_key(identifier, ip)
    with _attempts_lock:
        attempts = _load_attempts()
        if key in attempts:
            attempts.pop(key, None)
            _save_attempts(attempts)


# ── CROSS-SITE REQUEST FORGERY: ORIGIN/REFERER CHECK ──
# Complements (does not replace) the per-form CSRF tokens already used on
# the most sensitive forms (login, register, change-password, backup/
# restore, profile update) and the session cookie's SameSite=Lax flag,
# which already stops it riding along on a cross-site POST in any modern
# browser. Checking Origin/Referer closes the rest: every other
# state-changing POST route in the app (customers/items/quotations/
# proforma/packing/salary/user-management CRUD) — without having to
# thread a token through every one of those forms individually, and
# without a gap opening up the next time a new POST route is added,
# since this runs once, centrally, for every POST request.
def is_same_origin(headers, request_path=''):
    """True if this POST request's Origin/Referer (when present) names
    this same site. A real cross-site page cannot spoof Origin — it's one
    of the handful of headers browsers set themselves and refuse to let
    script override — so a mismatch here is a reliable sign of a forged
    cross-site submission. Neither header is sent by every possible
    client (some non-browser tools, very old browsers), so their total
    absence is allowed through rather than blocked, relying on
    SameSite=Lax as the backstop for that slice of traffic."""
    host_candidates = {h.strip().lower() for h in (
        headers.get('Host', ''),
        # X-Forwarded-Host can be a comma-separated list, one entry per
        # proxy hop (each hop appends the host it was asked for) — take
        # every entry as a candidate rather than treating the whole,
        # unsplit string as a single (never-matching) host.
        *headers.get('X-Forwarded-Host', '').split(','),
    ) if h.strip()}
    if not host_candidates:
        return True  # nothing to compare against (unusual; don't false-positive)

    origin = headers.get('Origin')
    if origin:
        try:
            netloc = urllib.parse.urlparse(origin).netloc.lower()
        except ValueError:
            return False
        return netloc in host_candidates

    referer = headers.get('Referer')
    if referer:
        try:
            netloc = urllib.parse.urlparse(referer).netloc.lower()
        except ValueError:
            return False
        return netloc in host_candidates

    return True


# ── SECURITY RESPONSE HEADERS ──
# Applied to every response (see BaseMixin.send / serve_static). None of
# these depend on request content, so they're safe defaults everywhere:
#   X-Content-Type-Options   stops the browser "sniffing" a response into
#                             a different content type than declared
#                             (relevant since some responses are
#                             user-influenced HTML/JSON/PDF).
#   X-Frame-Options /
#   frame-ancestors           this app has no legitimate reason to be
#                             framed by another site — blocks clickjacking.
#   Referrer-Policy           don't leak full internal URLs (which can
#                             contain search terms, ids) to third-party
#                             link targets.
#   Permissions-Policy        opt this origin out of powerful browser
#                             features it never uses.
#   Cache-Control             authenticated pages shouldn't be cached by
#                             shared/browser caches after logout.
SECURITY_HEADERS = [
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'same-origin'),
    ('Permissions-Policy', 'geolocation=(), camera=(), microphone=()'),
    # 'unsafe-inline' on script/style is a pragmatic concession, not an
    # oversight: the UI relies throughout on inline <script> blocks and
    # onclick="..." handlers, and rewriting that to a nonce/hash-based
    # CSP is a front-end restructuring project of its own. Escaping user
    # data before it reaches HTML (see esc()/_layout()) is what actually
    # stops XSS; CSP here still earns its keep by blocking object/embed
    # content, framing, and any script/style/font source other than this
    # origin and the two CDNs the app already uses.
    ('Content-Security-Policy',
     "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
     "form-action 'self'; object-src 'none'; img-src 'self' data:; "
     "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
     "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
     "font-src 'self' https://cdn.jsdelivr.net"),
]

NO_STORE_HEADERS = [('Cache-Control', 'no-store')]


# ── REQUEST PARSER ──
class FormData(dict):
    """Form data dict that supports repeated field names via getlist()."""

    def getlist(self, key):
        vals = []
        for k in self:
            if k == key:
                v = self[k]
                if isinstance(v, list):
                    vals.extend(v)
                else:
                    vals.append(v)
        return vals


def parse_form(body, content_type):
    if 'application/json' in content_type:
        if body:
            try:
                parsed = json.loads(body)
            except Exception:
                return FormData()
            # A JSON array (including one of 2-element arrays, which
            # dict()/FormData() would otherwise happily accept as
            # key/value pairs) is not a form and must be rejected rather
            # than silently turned into an unintended mapping.
            if not isinstance(parsed, dict):
                return FormData()
            return FormData(parsed)
        return FormData()
    if 'application/x-www-form-urlencoded' in content_type:
        # parse_qsl() on a bytes body returns bytes keys/values (e.g.
        # {b'field': b'val'}), which silently break every str-keyed
        # form.get('field') lookup elsewhere in the app. Decode to str
        # first so callers get the str-keyed dict they expect regardless
        # of whether the caller passed bytes or str.
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8', errors='replace')
        fd = FormData()
        # keep_blank_values=True is required here: proforma/quotation/
        # packing item rows are submitted as several parallel arrays
        # (item_id[], item_name[], description[], ...) that are zipped
        # together by position server-side. A blank value (e.g. a manually
        # entered item with no catalog item_id, or an item with no
        # description) is a legitimate, meaningful empty string for that
        # row — not "field absent". The default keep_blank_values=False
        # would silently drop just that one key/value pair, shortening
        # only that array and desyncing every row's fields from each
        # other (an item's rate ending up paired with the next item's
        # name, etc.) without raising any error.
        for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True):
            if k in fd:
                existing = fd[k]
                if isinstance(existing, list):
                    existing.append(v)
                else:
                    fd[k] = [existing, v]
            else:
                fd[k] = v
        return fd
    return FormData()


class UploadedFile:
    """One file part of a multipart/form-data submission — the raw bytes
    exactly as uploaded (never decoded as text), plus the filename and
    content-type the browser sent. Used for anything binary, e.g.
    restoring a database backup."""

    def __init__(self, filename, content_type, data):
        self.filename = filename
        self.content_type = content_type
        self.data = data

    def __repr__(self):
        return f"UploadedFile(filename={self.filename!r}, {len(self.data)} bytes)"


_MULTIPART_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;]+))')
_MULTIPART_NAME_RE = re.compile(r'\bname="([^"]*)"')
_MULTIPART_FILENAME_RE = re.compile(r'\bfilename="([^"]*)"')


def parse_multipart(body, content_type):
    """Parse a multipart/form-data body (raw bytes — must NOT be decoded
    as text first, since file parts can be arbitrary binary). Text fields
    come back as plain strings; file fields come back as UploadedFile
    instances so their bytes are never mangled by a text decode/re-encode
    round-trip."""
    fd = FormData()
    m = _MULTIPART_BOUNDARY_RE.search(content_type or '')
    if not m or not isinstance(body, (bytes, bytearray)):
        return fd
    boundary = (m.group(1) or m.group(2)).strip()
    delimiter = ('--' + boundary).encode('latin-1')

    for raw_part in body.split(delimiter):
        part = raw_part
        if part in (b'', b'--') or part.startswith(b'--'):
            continue
        # Each part begins with the CRLF that immediately follows the
        # boundary delimiter. Strip exactly that one leading CRLF — not
        # part.strip(b'\r\n'), which removes *every* trailing \r/\n byte
        # and will truncate binary content (e.g. a .db.enc backup's last
        # bytes happening to be 0x0d/0x0a) as well as double-strip the
        # trailing CRLF handled below.
        if part.startswith(b'\r\n'):
            part = part[2:]
        if b'\r\n\r\n' not in part:
            continue
        header_blob, content = part.split(b'\r\n\r\n', 1)
        # Trailing CRLF before the next boundary delimiter is part of the
        # delimiter, not the field's content.
        if content.endswith(b'\r\n'):
            content = content[:-2]

        headers = {}
        for line in header_blob.split(b'\r\n'):
            if b':' in line:
                k, v = line.split(b':', 1)
                headers[k.strip().lower().decode('latin-1')] = v.strip().decode('latin-1')

        disp = headers.get('content-disposition', '')
        name_m = _MULTIPART_NAME_RE.search(disp)
        if not name_m:
            continue
        field_name = name_m.group(1)

        filename_m = _MULTIPART_FILENAME_RE.search(disp)
        if filename_m is not None:
            value = UploadedFile(
                filename=filename_m.group(1),
                content_type=headers.get('content-type', 'application/octet-stream'),
                data=bytes(content))
        else:
            value = content.decode('utf-8', errors='replace')

        # Repeated field names (e.g. item_id[], item_name[] parallel-array
        # rows, same as the urlencoded case in parse_form()) must collect
        # into a list rather than the last one silently overwriting all
        # the others.
        if field_name in fd:
            existing = fd[field_name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                fd[field_name] = [existing, value]
        else:
            fd[field_name] = value
    return fd


# ── RESPONSE HELPERS ──
def redirect(url):
    return None, 302, [('Location', url)]


def json_response(data, status=200):
    return json.dumps(data).encode(), status, [('Content-Type', 'application/json')]


def html_response(body, status=200):
    return body.encode() if isinstance(body, str) else body, status, [
        ('Content-Type', 'text/html; charset=utf-8')]


def _content_disposition(filename):
    """Build a safe `attachment; filename="..."` value. Strips CR/LF (which
    could otherwise inject additional header lines/values) and escapes
    embedded double quotes/backslashes, so a dynamic filename containing
    `"`, `\r`, or `\n` can't break out of the quoted-string or corrupt the
    header."""
    safe = (filename or '').replace('\r', '').replace('\n', '')
    safe = safe.replace('\\', '\\\\').replace('"', '\\"')
    return f'attachment; filename="{safe}"'


def pdf_response(data, filename):
    return data, 200, [
        ('Content-Type', 'application/pdf'),
        ('Content-Disposition', _content_disposition(filename))]


def file_response(data, filename, content_type='application/octet-stream'):
    """Generic downloadable-file response (backups, data exports, etc.) —
    same shape as pdf_response but for any content type."""
    return data, 200, [
        ('Content-Type', content_type),
        ('Content-Disposition', _content_disposition(filename))]
