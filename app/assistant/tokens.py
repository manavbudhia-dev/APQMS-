"""
API tokens for the /api/v1 JSON API, plus a tiny per-chat context store
(so a bot user can say "pdf bhejo" right after creating a quotation).

Tokens are shown once at creation and stored only as a SHA-256 hash. Each
token belongs to a user; documents created with it are recorded as made by
that user, and a deactivated user's tokens stop working.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from database import get_db

TOKEN_PREFIX = 'apqms_'

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        db = get_db()
        try:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                token_hash TEXT UNIQUE NOT NULL,
                token_hint TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                revoked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS assistant_context (
                chat_key TEXT PRIMARY KEY,
                kind TEXT,
                doc_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            db.commit()
        finally:
            db.close()
        _schema_ready = True


def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_token(user_id, name):
    """-> (token_id, plaintext_token). The plaintext is never stored."""
    ensure_schema()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    db = get_db()
    try:
        tid = db.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, token_hint) VALUES (?,?,?,?)",
            (user_id, (name or 'token').strip()[:100] or 'token', _hash(token),
             token[:len(TOKEN_PREFIX) + 4] + '…' + token[-4:])).lastrowid
        db.commit()
    finally:
        db.close()
    return tid, token


def list_tokens(user_id=None):
    ensure_schema()
    db = get_db()
    try:
        sql = ("SELECT t.id, t.name, t.token_hint, t.created_at, t.last_used_at, t.revoked, "
               "u.username FROM api_tokens t JOIN users u ON u.id=t.user_id")
        params = ()
        if user_id is not None:
            sql += " WHERE t.user_id=?"
            params = (user_id,)
        return [dict(r) for r in db.execute(sql + " ORDER BY t.id DESC", params).fetchall()]
    finally:
        db.close()


def revoke_token(token_id, user_id=None):
    ensure_schema()
    db = get_db()
    try:
        sql, params = "UPDATE api_tokens SET revoked=1 WHERE id=?", [token_id]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        n = db.execute(sql, params).rowcount
        db.commit()
        return n > 0
    finally:
        db.close()


def authenticate(auth_header):
    """'Bearer apqms_...' -> user dict (id, username, role) or None."""
    if not auth_header:
        return None
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    token = parts[1].strip()
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    ensure_schema()
    h = _hash(token)
    db = get_db()
    try:
        row = db.execute(
            "SELECT t.id tid, t.token_hash, u.id, u.username, u.role, COALESCE(u.is_active,1) active "
            "FROM api_tokens t JOIN users u ON u.id=t.user_id "
            "WHERE t.token_hash=? AND t.revoked=0", (h,)).fetchone()
        if not row or not row['active'] or not hmac.compare_digest(row['token_hash'], h):
            return None
        db.execute("UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row['tid'],))
        db.commit()
        return {'id': row['id'], 'username': row['username'], 'role': row['role']}
    finally:
        db.close()


# ── chat context: "last document this chat worked with" ──────
def remember(chat_key, kind, doc_id):
    if not chat_key:
        return
    ensure_schema()
    db = get_db()
    try:
        db.execute("INSERT INTO assistant_context (chat_key, kind, doc_id, updated_at) "
                   "VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_key) DO UPDATE SET "
                   "kind=excluded.kind, doc_id=excluded.doc_id, updated_at=CURRENT_TIMESTAMP",
                   (str(chat_key)[:100], kind, doc_id))
        db.commit()
    finally:
        db.close()


def recall(chat_key):
    """-> (kind, doc_id) or (None, None)"""
    if not chat_key:
        return None, None
    ensure_schema()
    db = get_db()
    try:
        row = db.execute("SELECT kind, doc_id FROM assistant_context WHERE chat_key=?",
                         (str(chat_key)[:100],)).fetchone()
    finally:
        db.close()
    return (row['kind'], row['doc_id']) if row else (None, None)


# ── pending question per chat (e.g. "did you mean customer X?") ──
PENDING_MINUTES = 30


def _ensure_pending():
    db = get_db()
    try:
        db.execute("CREATE TABLE IF NOT EXISTS assistant_pending (chat_key TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        db.commit()
    finally:
        db.close()


def set_pending(chat_key, payload):
    """Store a JSON-able payload for this chat; None clears it."""
    if not chat_key:
        return
    import json
    _ensure_pending()
    db = get_db()
    try:
        if payload is None:
            db.execute("DELETE FROM assistant_pending WHERE chat_key=?", (str(chat_key)[:100],))
        else:
            db.execute("INSERT INTO assistant_pending (chat_key, payload, updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
                       "ON CONFLICT(chat_key) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
                       (str(chat_key)[:100], json.dumps(payload)))
        db.commit()
    finally:
        db.close()


def get_pending(chat_key):
    if not chat_key:
        return None
    import json
    _ensure_pending()
    db = get_db()
    try:
        row = db.execute("SELECT payload FROM assistant_pending WHERE chat_key=? AND "
                         "updated_at >= datetime('now', ?)",
                         (str(chat_key)[:100], f'-{PENDING_MINUTES} minutes')).fetchone()
    finally:
        db.close()
    if not row:
        return None
    try:
        return json.loads(row['payload'])
    except ValueError:
        return None
