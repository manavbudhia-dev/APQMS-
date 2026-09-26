from utils.http_utils import redirect
from utils.http_utils import update_session
from database import get_db
from database import get_or_create_financial_year


def _safe_next(form):
    """Only ever redirect back to a path on this site — never to an
    attacker-supplied absolute/protocol-relative URL."""
    next_url = (form.get('next') or '').strip()
    if not next_url or not next_url.startswith('/') or next_url.startswith('//'):
        return '/dashboard'
    return next_url


class FinancialYearPagesMixin:
    def handle_fy_select(self, form, user_id):
        """Switch the financial year scoping every quotation/proforma/
        packing list list & the dashboard for this session. 'all' clears
        the filter (shows every year); anything else must be a real
        financial_years id or the selection is left untouched."""
        next_url = _safe_next(form)
        raw = (form.get('financial_year_id') or '').strip()
        if raw == 'all':
            update_session(self.headers.get('Cookie'), fy_id='all')
        elif raw.isdigit():
            db = get_db()
            try:
                exists = db.execute(
                    "SELECT 1 FROM financial_years WHERE id=?", (raw,)).fetchone()
            finally:
                db.close()
            if exists:
                update_session(self.headers.get('Cookie'), fy_id=int(raw))
        return self.send(*redirect(next_url))

    def handle_fy_add(self, form, user_id):
        """Create (or reuse, if it already exists) the Indian financial
        year starting 1 April of the given year, then switch to it."""
        next_url = _safe_next(form)
        raw_year = (form.get('start_year') or '').strip()
        try:
            start_year = int(raw_year)
        except (TypeError, ValueError):
            return self.send(*redirect(next_url))
        if start_year < 2000 or start_year > 2099:
            return self.send(*redirect(next_url))

        db = get_db()
        try:
            fy = get_or_create_financial_year(db, start_year)
            db.commit()
        finally:
            db.close()

        if fy:
            update_session(self.headers.get('Cookie'), fy_id=fy['id'])
        return self.send(*redirect(next_url))
