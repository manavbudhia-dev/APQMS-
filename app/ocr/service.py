"""
Scan & Import — storage and processing.

Sits between the web pages (app/views/ocr_pages.py) and the pure logic
(engine / parser / matching). It owns:

* saving the uploaded original to disk (random name, owner-only permissions,
  never served by name — only through an authenticated route that looks the
  file up by document id),
* running extraction in a background thread, because OCR of a multi-page scan
  takes many seconds and a web request would time out (or hold a worker),
* writing the result to `ocr_documents` / `ocr_document_items`.

Each processing thread opens its own SQLite connection (connections must not
be shared across threads). At most OCR_MAX_CONCURRENT documents are OCR'd at
once; others wait their turn so a burst of uploads can't saturate the CPU.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import traceback

from database import DB_PATH, get_db

from . import engine, matching, parser

UPLOAD_DIR = os.environ.get('QUOTATION_OCR_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), 'ocr_uploads')

try:
    _MAX_CONCURRENT = max(1, int(os.environ.get('OCR_MAX_CONCURRENT', '2')))
except ValueError:
    _MAX_CONCURRENT = 2
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)

STALE_MINUTES = 15
FLAG_SEP = ' | '

STATUS_PROCESSING, STATUS_READY, STATUS_SAVED = 'Processing', 'Ready', 'Saved'
STATUS_QUOTED, STATUS_FAILED = 'Quotation Created', 'Failed'

CONTENT_TYPES = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg',
                 '.tif': 'image/tiff', '.bmp': 'image/bmp', '.webp': 'image/webp',
                 '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}


# ── Files ────────────────────────────────────────────────────────────
def _store_file(data, ext):
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    name = secrets.token_hex(16) + ext
    fd = os.open(os.path.join(UPLOAD_DIR, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return name


def _path_for(stored_filename):
    """Absolute path of a stored upload; refuses anything that isn't a plain
    filename inside UPLOAD_DIR."""
    if not stored_filename or stored_filename != os.path.basename(stored_filename):
        return None
    return os.path.join(UPLOAD_DIR, stored_filename)


def read_original(stored_filename):
    p = _path_for(stored_filename)
    if p and os.path.isfile(p):
        with open(p, 'rb') as f:
            return f.read()
    return None


def safe_download_name(name):
    """A filename that is safe to place inside a Content-Disposition header."""
    base = os.path.basename((name or 'document').replace('\\', '/'))
    base = re.sub(r'[^A-Za-z0-9._ \-]+', '_', base).strip(' .') or 'document'
    return base[:120]


# ── Create / start ───────────────────────────────────────────────────
def create_document(original_filename, kind, ext, data, user_id):
    stored = _store_file(data, ext)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO ocr_documents (original_filename, stored_filename, file_type, file_size, "
            "status, created_by) VALUES (?,?,?,?,?,?)",
            (safe_download_name(original_filename), stored, kind, len(data), STATUS_PROCESSING, user_id))
        db.commit()
        return cur.lastrowid
    except Exception:
        db.rollback()
        p = _path_for(stored)
        if p and os.path.isfile(p):
            os.remove(p)
        raise
    finally:
        db.close()


def start_processing(doc_id, force_ocr=False):
    threading.Thread(target=_run, args=(doc_id, force_ocr), daemon=True,
                     name=f'ocr-{doc_id}').start()


def _run(doc_id, force_ocr):
    with _slots:
        process(doc_id, force_ocr)


# ── Item construction ────────────────────────────────────────────────
def _split_name_description(text):
    """Unmatched item: first line is the name, the rest the description —
    unless the rest is just a one/two-word wrap fragment ('Pitch'), which is
    the tail of the same sentence."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return '', ''
    name, rest = lines[0], lines[1:]
    if len(rest) == 1 and len(rest[0].split()) <= 2 and not re.search(r'\d', rest[0]):
        return f'{name} {rest[0]}', ''
    return name, '\n'.join(rest)


def build_item(pi, index):
    """ParsedItem -> dict of ocr_document_items columns, linked to the
    catalog when there's a confident match."""
    first_line = pi.description.split('\n')[0]
    row, score = index.best(pi.description)
    row2, score2 = index.best(first_line)          # the name line alone copes with extra spec lines
    if score2 > score:
        row, score = row2, score2

    flags = list(pi.flags)
    if row is not None:
        name, desc, spec = row['item_name'], row['description'] or '', row['specification'] or ''
        catalog_rate = row['rate_per_unit'] or None
        unit = pi.unit or row['unit'] or ''
        item_id = row['id']
    else:
        name, desc = _split_name_description(pi.description)
        spec, catalog_rate, unit, item_id = '', None, pi.unit, None
        if not unit:
            flags.append('No unit found')
    rate = pi.rate if pi.rate else catalog_rate
    if row is not None and not pi.rate and not catalog_rate:
        flags.append('Catalog item has no rate set')
    return {'item_id': item_id, 'item_name': name, 'description': desc, 'specification': spec,
            'quantity': pi.quantity, 'unit': unit, 'rate': rate, 'catalog_rate': catalog_rate,
            'match_score': score, 'flags': FLAG_SEP.join(flags), 'raw_text': pi.raw}


# ── Processing ───────────────────────────────────────────────────────
def _fail(db, doc_id, message):
    db.execute("UPDATE ocr_documents SET status=?, error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (STATUS_FAILED, message[:500], doc_id))
    db.commit()


def process(doc_id, force_ocr=False):
    """Read the stored file, extract + parse it, save the outcome. Never
    raises: any failure is recorded on the document instead."""
    db = get_db()
    try:
        doc = db.execute("SELECT * FROM ocr_documents WHERE id=?", (doc_id,)).fetchone()
        if doc is None:
            return
        data = read_original(doc['stored_filename'])
        if data is None:
            return _fail(db, doc_id, 'The uploaded file is no longer on the server.')

        result = engine.extract(data, doc['file_type'], force_ocr=force_ocr)
        parsed = parser.parse(result.lines)

        catalog = db.execute(
            "SELECT id, item_name, description, specification, unit, rate_per_unit FROM items").fetchall()
        customers = db.execute("SELECT id, company_name, gstin FROM customers").fetchall()
        index = matching.CatalogIndex(catalog)
        items = [build_item(pi, index) for pi in parsed.items]
        cust, how = matching.match_customer(parsed.gstin, parsed.company_name, customers)

        subject = parsed.subject
        if not subject:
            names = []
            for it in items:
                if it['item_id'] and it['item_name'] not in names:
                    names.append(it['item_name'])
            subject = ', '.join(names[:3])

        warnings = list(result.warnings) + list(parsed.warnings)
        db.execute(
            "UPDATE ocr_documents SET status=?, page_count=?, extraction_method=?, ocr_confidence=?, "
            "raw_text=?, warnings=?, customer_id=?, customer_match=?, company_name=?, contact_person=?, "
            "address=?, phone=?, email=?, gstin=?, doc_reference=?, doc_date=?, subject=?, error_message=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (STATUS_READY, result.page_count, result.method, result.avg_confidence, result.text,
             json.dumps(warnings), cust['id'] if cust else None, how, parsed.company_name,
             parsed.contact_person, parsed.address, parsed.phone, parsed.email, parsed.gstin,
             parsed.reference, parsed.date, subject, doc_id))
        db.execute("DELETE FROM ocr_document_items WHERE ocr_document_id=?", (doc_id,))
        for n, it in enumerate(items):
            db.execute(
                "INSERT INTO ocr_document_items (ocr_document_id, item_id, item_name, description, "
                "specification, quantity, unit, rate, catalog_rate, match_score, flags, raw_text, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, it['item_id'], it['item_name'], it['description'], it['specification'],
                 it['quantity'], it['unit'], it['rate'], it['catalog_rate'], it['match_score'],
                 it['flags'], it['raw_text'], n))
        db.commit()
    except (engine.OcrError, engine.OcrUnavailableError) as e:
        db.rollback()
        _fail(db, doc_id, str(e))
    except Exception:  # noqa: BLE001 — recorded, never shown raw to the user
        db.rollback()
        print(f'[ocr] unexpected failure on document {doc_id}:', file=sys.stderr)
        traceback.print_exc()
        _fail(db, doc_id, 'Something went wrong while reading this file. The details were '
                          'written to the server log.')
    finally:
        db.close()


# ── Queries and updates used by the pages ────────────────────────────
def reap_stale(db):
    """A 'Processing' document older than STALE_MINUTES lost its worker
    (server restart / crash) — mark it failed so it doesn't spin forever.

    Runs on every list/detail view, so the recovery UPDATE is only issued
    when something actually needs recovering: an UPDATE that matches no
    rows still takes the database write lock, which made simply looking at
    the Scan & Import page contend with unrelated saves."""
    stale = db.execute(
        "SELECT 1 FROM ocr_documents WHERE status=? "
        "AND created_at < datetime('now', ?) LIMIT 1",
        (STATUS_PROCESSING, f'-{STALE_MINUTES} minutes')).fetchone()
    if stale is None:
        return
    db.execute(
        "UPDATE ocr_documents SET status=?, error_message=? WHERE status=? "
        "AND created_at < datetime('now', ?)",
        (STATUS_FAILED, 'Processing was interrupted (the server may have restarted). '
                        'Please upload the file again.', STATUS_PROCESSING, f'-{STALE_MINUTES} minutes'))
    db.commit()


def list_documents(db, limit=100):
    reap_stale(db)
    return db.execute(
        "SELECT d.id, d.original_filename, d.file_type, d.status, d.ocr_confidence, d.company_name, "
        "d.created_at, d.quotation_id, d.created_by, u.username AS uploader, "
        "c.company_name AS customer_name, q.quotation_no, "
        "(SELECT COUNT(*) FROM ocr_document_items i WHERE i.ocr_document_id=d.id) AS item_count "
        "FROM ocr_documents d LEFT JOIN users u ON u.id=d.created_by "
        "LEFT JOIN customers c ON c.id=d.customer_id LEFT JOIN quotations q ON q.id=d.quotation_id "
        "ORDER BY d.id DESC LIMIT ?", (limit,)).fetchall()


def get_document(db, doc_id):
    reap_stale(db)
    return db.execute(
        "SELECT d.*, q.quotation_no FROM ocr_documents d "
        "LEFT JOIN quotations q ON q.id=d.quotation_id WHERE d.id=?", (doc_id,)).fetchone()


def get_items(db, doc_id):
    return db.execute("SELECT * FROM ocr_document_items WHERE ocr_document_id=? "
                      "ORDER BY sort_order, id", (doc_id,)).fetchall()


HEADER_FIELDS = ('company_name', 'contact_person', 'address', 'phone', 'email', 'gstin',
                 'doc_reference', 'doc_date', 'subject')


def save_review(db, doc_id, customer_id, header, rows, status):
    """Store the reviewer's edits. Rows carrying an existing `row_id` are
    updated in place (keeping their raw text / flags / match score); rows
    without one are new; existing rows that were removed are deleted.
    Does NOT commit — the caller decides (quotation creation does more)."""
    sets = ', '.join(f'{f}=?' for f in HEADER_FIELDS)
    db.execute(f"UPDATE ocr_documents SET customer_id=?, {sets}, status=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (customer_id, *[header.get(f, '') for f in HEADER_FIELDS], status, doc_id))
    existing = {r['id'] for r in db.execute(
        "SELECT id FROM ocr_document_items WHERE ocr_document_id=?", (doc_id,))}
    keep = set()
    for n, r in enumerate(rows):
        vals = (r['item_id'], r['item_name'], r['description'], r['specification'],
                r['quantity'], r['unit'], r['rate'], n)
        if r.get('row_id') in existing:
            keep.add(r['row_id'])
            db.execute("UPDATE ocr_document_items SET item_id=?, item_name=?, description=?, "
                       "specification=?, quantity=?, unit=?, rate=?, sort_order=? WHERE id=?",
                       (*vals, r['row_id']))
        else:
            db.execute("INSERT INTO ocr_document_items (item_id, item_name, description, specification, "
                       "quantity, unit, rate, sort_order, ocr_document_id) VALUES (?,?,?,?,?,?,?,?,?)",
                       (*vals, doc_id))
    for rid in existing - keep:
        db.execute("DELETE FROM ocr_document_items WHERE id=?", (rid,))


def delete_document(db, doc_id):
    row = db.execute("SELECT stored_filename FROM ocr_documents WHERE id=?", (doc_id,)).fetchone()
    db.execute("DELETE FROM ocr_documents WHERE id=?", (doc_id,))
    db.commit()
    if row:
        p = _path_for(row['stored_filename'])
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
