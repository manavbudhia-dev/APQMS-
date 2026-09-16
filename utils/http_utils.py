"""
Shared, dependency-free helpers used across all page/handler modules:
HTML escaping, currency formatting, the in-memory session store, request
form parsing, small response-tuple builders (body, status, headers), and
basic auth-security helpers (CSRF tokens, login rate limiting).
"""
import json
import os
import secrets
import time
import urllib.parse
import html as _html
from http.cookies import SimpleCookie
from threading import Lock


def esc(s):
    """HTML-escape a value for safe embedding in markup/attributes."""
    return _html.escape(str(s), quote=True)


def esc_nl(s):
    """HTML-escape a value and turn newlines into <br> for read-only display
    of multi-line fields (e.g. a customer's address entered line by line)."""
    return esc(s).replace('\n', '<br>')


def fmt_rupee(n):
    """Format a number with Indian digit grouping."""
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "0"


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
    tmp_path = _SESSIONS_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(sessions, f)
    os.replace(tmp_path, _SESSIONS_FILE)  # atomic on POSIX


def get_session(cookie_header):
    if not cookie_header:
        return {}
    c = SimpleCookie()
    c.load(cookie_header)
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


def destroy_session(cookie_header):
    """Remove a session, e.g. on explicit logout."""
    if not cookie_header:
        return
    c = SimpleCookie()
    c.load(cookie_header)
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
    tmp_path = _CSRF_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(tokens, f)
    os.replace(tmp_path, _CSRF_FILE)


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
    tmp_path = _ATTEMPTS_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(attempts, f)
    os.replace(tmp_path, _ATTEMPTS_FILE)


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
    with _attempts_lock:
        attempts = _load_attempts()
        entry = attempts.get(key, {'count': 0, 'last': 0})
        entry['count'] += 1
        entry['last'] = time.time()
        attempts[key] = entry
        _save_attempts(attempts)


def clear_failed_logins(identifier, ip):
    key = _attempt_key(identifier, ip)
    with _attempts_lock:
        attempts = _load_attempts()
        if key in attempts:
            attempts.pop(key, None)
            _save_attempts(attempts)


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
                return FormData(json.loads(body))
            except Exception:
                return FormData()
        return FormData()
    if 'application/x-www-form-urlencoded' in content_type:
        fd = FormData()
        for k, v in urllib.parse.parse_qsl(body):
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


# ── RESPONSE HELPERS ──
def redirect(url):
    return None, 302, [('Location', url)]


def json_response(data, status=200):
    return json.dumps(data).encode(), status, [('Content-Type', 'application/json')]


def html_response(body, status=200):
    return body.encode() if isinstance(body, str) else body, status, [
        ('Content-Type', 'text/html; charset=utf-8')]


def pdf_response(data, filename):
    return data, 200, [
        ('Content-Type', 'application/pdf'),
        ('Content-Disposition', f'attachment; filename="{filename}"')]
