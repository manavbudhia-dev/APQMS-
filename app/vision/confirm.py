"""
Confirmed photo values -> quotation. Shared by the web review screen and
the Telegram bot so both apply exactly the same rules.
"""
from __future__ import annotations

from database import get_db
from app.assistant import core
from app.vision import store
from app.vision.schema import FIELD_LABELS, REQUIRED_FIELDS


class ConfirmError(Exception):
    def __init__(self, errors):
        super().__init__('; '.join(errors))
        self.errors = errors


def _fmt(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def check_values(values):
    """-> list of problems that block creating a quotation."""
    errors = []
    for f in REQUIRED_FIELDS:
        if values.get(f) in (None, ''):
            errors.append(f'{FIELD_LABELS[f]} zaroori hai.')
    for f in ('rate_per_mtr', 'quantity_mtrs', 'delivery_weeks'):
        if values.get(f) is not None and values[f] <= 0:
            errors.append(f'{FIELD_LABELS[f]} 0 se zyada honi chahiye.')
    return errors


def create_quotation(rid, values, customer_id, user_id, source='Photo Quote'):
    """Create the quotation for extraction `rid`. Returns the document dict.
    Raises ConfirmError. Safe against double submits: the row is claimed
    first, so a second call returns the existing quotation instead."""
    row = store.get(rid)
    if row is None:
        raise ConfirmError(['Photo record nahi mila.'])
    if row['quotation_id']:
        return core.get_document('quotation', row['quotation_id'])
    errors = check_values(values)
    if not customer_id:
        errors.append('Customer choose karein.')
    if errors:
        raise ConfirmError(errors)

    db = get_db()
    try:
        claimed = db.execute("UPDATE vision_extractions SET status='confirming' WHERE id=? "
                             "AND quotation_id IS NULL AND status IN (?,?)",
                             (rid, store.STATUS_EXTRACTED, store.STATUS_FAILED)).rowcount
        db.commit()
    finally:
        db.close()
    if not claimed:
        row = store.get(rid)
        if row and row['quotation_id']:
            return core.get_document('quotation', row['quotation_id'])
        raise ConfirmError(['Ye photo abhi process ho rahi hai — thodi der baad dekhein.'])

    payload = store.quotation_payload(values)
    payload['customer_id'] = customer_id
    note = f'Created from {source} #{rid} ({row["original_filename"] or "photo"}).'
    if values.get('original_rate_per_mtr') is not None:
        note += (f' Rate on sheet revised from {_fmt(values["original_rate_per_mtr"])} '
                 f'to {_fmt(values["rate_per_mtr"])} per mtr.')
    payload['notes'] = note
    try:
        doc = core.create_document('quotation', payload, user_id)
    except core.AssistantError as e:
        db = get_db()
        try:
            db.execute("UPDATE vision_extractions SET status=? WHERE id=?", (row['status'], rid))
            db.commit()
        finally:
            db.close()
        raise ConfirmError([e.message])
    if values.get('gst_percent') is not None and values['gst_percent'] != core.GST_PERCENT:
        db = get_db()
        try:
            db.execute("UPDATE quotation_items SET gst_percent=? WHERE quotation_id=?",
                       (values['gst_percent'], doc['id']))
            db.commit()
        finally:
            db.close()
        doc = core.get_document('quotation', doc['id'])
    store.mark_confirmed(rid, values, user_id, doc['id'])
    return doc
