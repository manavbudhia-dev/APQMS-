"""
Audit trail for photo quotations: the original photo (bytes exactly as
uploaded) is kept on disk next to the database, and one row per upload
records the raw model output, the validated extraction, the values the
user confirmed, who confirmed them, and the quotation that was made.

Also maps confirmed values onto the app's normal quotation (one line item
plus terms), so the PDF comes from the existing quotation template.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

from database import DB_PATH, get_db
from app.vision.schema import empty_extraction

UPLOAD_DIR = os.environ.get('QUOTATION_VISION_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), 'vision_uploads')
EXT = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}

STATUS_EXTRACTED = 'extracted'
STATUS_FAILED = 'failed'
STATUS_CONFIRMED = 'confirmed'

_ready = False
_lock = threading.Lock()


def ensure_schema():
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        db = get_db()
        try:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS vision_extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                original_filename TEXT,
                stored_filename TEXT,
                media_type TEXT,
                image_sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'extracted',
                model TEXT,
                attempts INTEGER,
                usage_json TEXT,
                raw_response TEXT,
                extracted_json TEXT,
                error TEXT,
                confirmed_json TEXT,
                confirmed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                confirmed_at TIMESTAMP,
                quotation_id INTEGER REFERENCES quotations(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_vision_extractions_created_by ON vision_extractions(created_by);
            """)
            cols = {r[1] for r in db.execute("PRAGMA table_info(vision_extractions)").fetchall()}
            for col, typ in (('source', "TEXT DEFAULT 'photo'"), ('source_text', 'TEXT'),
                             ('draft_json', 'TEXT')):
                if col not in cols:     # 'text' rows: values read from text pasted in the bot
                    db.execute(f"ALTER TABLE vision_extractions ADD COLUMN {col} {typ}")
            db.commit()
        finally:
            db.close()
        _ready = True


def create(user_id, filename, media_type, data):
    """Store the original photo; -> new row id."""
    ensure_schema()
    sha = hashlib.sha256(data).hexdigest()
    db = get_db()
    try:
        rid = db.execute(
            "INSERT INTO vision_extractions (created_by, original_filename, media_type, image_sha256, status) "
            "VALUES (?,?,?,?,?)",
            (user_id, (filename or 'photo')[:200], media_type, sha, STATUS_EXTRACTED)).lastrowid
        name = f'{rid}_{sha[:12]}.{EXT.get(media_type, "bin")}'
        os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(UPLOAD_DIR, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        db.execute("UPDATE vision_extractions SET stored_filename=? WHERE id=?", (name, rid))
        db.commit()
        return rid
    finally:
        db.close()


def create_text(user_id, text, label='Telegram text'):
    """Record text the user pasted (instead of a photo); -> new row id."""
    ensure_schema()
    db = get_db()
    try:
        rid = db.execute(
            "INSERT INTO vision_extractions (created_by, original_filename, status, source, source_text, "
            "image_sha256) VALUES (?,?,?,?,?,?)",
            (user_id, label[:200], STATUS_EXTRACTED, 'text', text,
             hashlib.sha256(text.encode('utf-8')).hexdigest())).lastrowid
        db.commit()
        return rid
    finally:
        db.close()


def save_draft(rid, values):
    db = get_db()
    try:
        db.execute("UPDATE vision_extractions SET draft_json=? WHERE id=?", (json.dumps(values), rid))
        db.commit()
    finally:
        db.close()


def save_result(rid, result):
    db = get_db()
    try:
        db.execute("UPDATE vision_extractions SET status=?, model=?, attempts=?, usage_json=?, raw_response=?, "
                   "extracted_json=?, error=NULL WHERE id=?",
                   (STATUS_EXTRACTED, result.model, result.attempts, json.dumps(result.usage),
                    result.raw_text, json.dumps(result.data), rid))
        db.commit()
    finally:
        db.close()


def save_error(rid, message, raw_text=''):
    db = get_db()
    try:
        db.execute("UPDATE vision_extractions SET status=?, error=?, raw_response=? WHERE id=?",
                   (STATUS_FAILED, message[:2000], raw_text or None, rid))
        db.commit()
    finally:
        db.close()


def get(rid):
    ensure_schema()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM vision_extractions WHERE id=?", (rid,)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    d = dict(row)
    d['extracted'] = json.loads(d['extracted_json']) if d.get('extracted_json') else empty_extraction()
    d['confirmed'] = json.loads(d['confirmed_json']) if d.get('confirmed_json') else None
    d['draft'] = json.loads(d['draft_json']) if d.get('draft_json') else None
    return d


def recent(limit=20):
    ensure_schema()
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT v.id, v.created_at, v.original_filename, v.status, v.quotation_id, v.error, "
            "q.quotation_no, u.username FROM vision_extractions v "
            "LEFT JOIN quotations q ON q.id=v.quotation_id LEFT JOIN users u ON u.id=v.created_by "
            "ORDER BY v.id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        db.close()


def read_image(row):
    name = (row or {}).get('stored_filename')
    if not name or '/' in name or '\\' in name or name.startswith('.'):
        return None
    try:
        with open(os.path.join(UPLOAD_DIR, name), 'rb') as f:
            return f.read()
    except OSError:
        return None


def mark_confirmed(rid, confirmed, user_id, quotation_id):
    db = get_db()
    try:
        db.execute("UPDATE vision_extractions SET status=?, confirmed_json=?, confirmed_by=?, "
                   "confirmed_at=CURRENT_TIMESTAMP, quotation_id=? WHERE id=?",
                   (STATUS_CONFIRMED, json.dumps(confirmed), user_id, quotation_id, rid))
        db.commit()
    finally:
        db.close()


def delete(rid):
    row = get(rid)
    if row is None:
        return
    if row.get('stored_filename') and read_image(row) is not None:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row['stored_filename']))
        except OSError:
            pass
    db = get_db()
    try:
        db.execute("DELETE FROM vision_extractions WHERE id=?", (rid,))
        db.commit()
    finally:
        db.close()


# ── confirmed values -> quotation fields ─────────────────────
def _fmt(n):
    return ('%f' % float(n)).rstrip('0').rstrip('.') if n is not None else ''


def quotation_payload(c):
    """Map confirmed values onto core.create_document()'s input: one line
    item (product + specification lines), terms, and notes."""
    spec = []
    if c.get('link_material'):
        spec.append(f"Link: forged from {c['link_material']}")
    if c.get('pin'):
        spec.append(f"Pin: {c['pin']}")
    att = c.get('attachment') or {}
    parts = []
    if att.get('material'):
        parts.append(f"MOC {att['material']}")
    if att.get('height_mm') is not None:
        parts.append(f"Height {_fmt(att['height_mm'])}mm")
    if att.get('thickness_mm') is not None:
        parts.append(f"Thickness {_fmt(att['thickness_mm'])}mm")
    if parts:
        spec.append('Attachment: ' + ', '.join(parts))
    if c.get('total_width_mm') is not None:
        spec.append(f"Total Width: {_fmt(c['total_width_mm'])}mm")

    terms = []
    if c.get('gst_percent') is not None:
        terms.append(f"GST {_fmt(c['gst_percent'])}% extra as applicable.")
    if c.get('price_basis'):
        terms.append(f"Rates {c['price_basis']}.")
    if c.get('delivery_weeks') is not None:
        terms.append(f"Delivery: {_fmt(c['delivery_weeks'])} weeks from the date of order.")
    if c.get('payment_terms'):
        terms.append(f"Payment: {c['payment_terms']}.".replace('..', '.'))
    terms_text = '\n'.join(f'{i}. {t}' for i, t in enumerate(terms, 1))

    return {
        'subject': c.get('product') or '',
        'terms': terms_text,
        'customer_notes': c.get('notes') or '',
        'items': [{
            'item_name': c.get('product') or 'Chain',
            'description': '',
            'specification': '\n'.join(spec),
            'quantity': c['quantity_mtrs'],
            'unit': 'Mtr.',
            'rate': c['rate_per_mtr'],
            'no_catalog': True,
        }],
    }
