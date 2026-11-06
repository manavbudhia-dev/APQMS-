import re
import sys
import traceback
import urllib.parse
from http.cookies import SimpleCookie

from utils.http_utils import redirect
from app.ocr.engine import MAX_UPLOAD_BYTES as _OCR_MAX_UPLOAD_BYTES
from utils.http_utils import json_response
from utils.http_utils import html_response
from utils.http_utils import get_session
from utils.http_utils import sessions
from utils.http_utils import parse_form
from utils.http_utils import parse_multipart
from utils.http_utils import is_same_origin
from database import get_db
from database import begin_immediate


_GENERIC_ERROR_PAGE = (
    '<!DOCTYPE html><html><head><meta charset="UTF-8">'
    '<title>Something went wrong</title></head><body style="font-family:sans-serif;'
    'max-width:560px;margin:80px auto;text-align:center;color:#334155">'
    '<h2 style="color:#DC2626">Something went wrong</h2>'
    '<p>An unexpected error occurred while handling your request. '
    'It has been logged. Please try again, and contact an administrator '
    'if the problem continues.</p>'
    '<p><a href="/dashboard">&larr; Back to Dashboard</a></p>'
    '</body></html>'
).encode()


# ── Pagination defaults for list-style API endpoints ─────────
# Any endpoint that can grow without bound (e.g. every item/customer
# in the DB) should never run an unbounded SELECT * — that's a memory
# and latency problem waiting to happen as data grows.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _pagination(qs):
    """Parse & clamp ?limit=&offset= from a parsed query string dict.

    Never trusts the client's numbers blindly: non-numeric values fall
    back to the default, and limit is always clamped to (1, MAX_LIMIT]
    so a client can't request the entire table in one shot.
    """
    def _int(key, default):
        raw = qs.get(key, [None])[0]
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    limit = max(1, min(_int('limit', DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, _int('offset', 0))
    return limit, offset


# ── Route tables ──────────────────────────────────────────────
# Each entry is (compiled_regex, handler). IDs are captured with \d+
# groups, so a match is only ever produced for a genuinely numeric
# segment — no more `int(path.split('/')[2])` that throws IndexError
# on a malformed/short path or ValueError on a non-numeric one, and
# no risk of the parser silently reading the wrong segment if a URL
# shape changes. Patterns are fully anchored (^...$), so ordering
# between unrelated routes doesn't matter; more specific routes
# (e.g. '/quotations/new') simply have a more specific pattern than
# the generic '/quotations/(\d+)' one, rather than relying on being
# checked first in an if/elif chain.
#
# GET handlers: handler(self, match, query_params, user_id)
# POST handlers: handler(self, match, form, user_id)

def _id(match, group=1):
    return int(match.group(group))


# Upload limit plus headroom for the multipart envelope and other form fields.
OCR_MAX_REQUEST_BYTES = _OCR_MAX_UPLOAD_BYTES + 1024 * 1024
# Hard cap on the body size accepted for every *other* POST route (login,
# settings, quotations, etc). Without a bound here, a client could point a
# multi-gigabyte Content-Length at any non-OCR route and force the whole
# thing into memory before auth/CSRF is even checked. Bump this (and add a
# route-specific check like the OCR one above) if a route legitimately
# needs to accept something larger than a normal form/backup-file body.
MAX_REQUEST_BYTES = 64 * 1024 * 1024
# Oversized uploads up to this size are read-and-discarded (in small chunks, so
# memory stays flat) before replying; beyond it the connection is just closed.
OCR_MAX_DRAIN_BYTES = 200 * 1024 * 1024


def _is_safe_backup_filename(name):
    """Guards the server-backup filename routes.

    The routes' regex already excludes '/', so multi-segment traversal
    like '../etc/passwd' can't reach here at all. But a bare '.' or '..'
    is still a single, otherwise-valid path *component* that resolves to
    the current/parent directory when joined onto a base directory — so
    reject any name that is made up entirely of dots.
    """
    return bool(name) and name.strip('.') != ''


class RoutingMixin:
    # ── GET ──────────────────────────────────────────────────
    GET_ROUTES = [
        (re.compile(r'^/dashboard$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_dashboard(uid)))),
        (re.compile(r'^/api/dashboard/quotations$'),
         lambda self, m, qs, uid: self.send(*json_response(self._dashboard_quotations_fragment(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             date_from=qs.get('date_from', [''])[0], date_to=qs.get('date_to', [''])[0],
             limit=qs.get('limit', [None])[0])))),

        # Customers
        (re.compile(r'^/customers$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_customers(
             q=qs.get('q', [''])[0], page=qs.get('page', ['1'])[0],
             sort=qs.get('sort', ['oldest'])[0], status=qs.get('status', ['all'])[0],
             page_size=qs.get('page_size', ['10'])[0],
             deleted=qs.get('deleted', ['0'])[0], blocked=qs.get('blocked', ['0'])[0])))),
        (re.compile(r'^/customers/(\d+)/products$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_customer_products(_id(m))))),

        # Items / Categories
        (re.compile(r'^/items$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_items(
             q=qs.get('q', [''])[0], page=qs.get('page', ['1'])[0],
             sort=qs.get('sort', ['name'])[0], direction=qs.get('direction', ['asc'])[0],
             type_filter=qs.get('type', [''])[0])))),
        (re.compile(r'^/categories$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_categories()))),
        (re.compile(r'^/categories/(\d+)/items$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_category_items(_id(m))))),

        # Settings
        (re.compile(r'^/settings$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_settings(uid)))),
        (re.compile(r'^/settings/backup$'),
         lambda self, m, qs, uid: self.send(*self._settings_backup(uid))),
        (re.compile(r'^/settings/backup/server/([\w.\-]+)$'),
         lambda self, m, qs, uid: self.send(*self._settings_server_backup_download(uid, m.group(1)))
         if _is_safe_backup_filename(m.group(1)) else self.send(*html_response(self.pg_404(), 404))),
        (re.compile(r'^/settings/export$'),
         lambda self, m, qs, uid: self.send(*self._settings_export(uid))),

        # Salary
        (re.compile(r'^/salary$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_list(qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/new$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_new(qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/(\d+)/attendance$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_attendance(_id(m), qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/(\d+)/edit$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_edit(_id(m), qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/(\d+)$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_detail(_id(m), qs.get('month', [None])[0])))),

        # Quotations
        (re.compile(r'^/quotations$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_quotations(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),
        (re.compile(r'^/quotations/new$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_qnew()))),
        (re.compile(r'^/quotations/(\d+)/pdf$'),
         lambda self, m, qs, uid: self.send(*self._gen_pdf(_id(m)))),
        (re.compile(r'^/quotations/(\d+)/edit$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_qedit(_id(m))))),
        # NB: GET .../delete intentionally does not delete anything (the
        # actual delete is a POST-only action) — preserved as-is.
        (re.compile(r'^/quotations/(\d+)/delete$'),
         lambda self, m, qs, uid: self.send(*redirect('/quotations'))),
        (re.compile(r'^/quotations/(\d+)/status$'),
         lambda self, m, qs, uid: self.send(*redirect(f'/quotations/{m.group(1)}'))),
        (re.compile(r'^/quotations/(\d+)$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_qdetail(_id(m))))),

        # Proforma Invoices
        (re.compile(r'^/proforma$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pilist(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),
        (re.compile(r'^/proforma/new$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pinew()))),
        (re.compile(r'^/proforma/(\d+)/pdf$'),
         lambda self, m, qs, uid: self.send(*self._gen_pi_pdf(_id(m)))),
        (re.compile(r'^/proforma/(\d+)/edit$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_piedit(_id(m))))),
        (re.compile(r'^/proforma/(\d+)/delete$'),
         lambda self, m, qs, uid: self.send(*redirect('/proforma'))),
        (re.compile(r'^/proforma/(\d+)/status$'),
         lambda self, m, qs, uid: self.send(*redirect(f'/proforma/{m.group(1)}'))),
        (re.compile(r'^/proforma/(\d+)$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pidetail(_id(m))))),

        # Packing Lists
        (re.compile(r'^/packing$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pllist(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),
        (re.compile(r'^/packing/new$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_plnew()))),
        (re.compile(r'^/packing/(\d+)/pdf$'),
         lambda self, m, qs, uid: self.send(*self._gen_pl_pdf(_id(m)))),
        (re.compile(r'^/packing/(\d+)/edit$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pledit(_id(m))))),
        (re.compile(r'^/packing/(\d+)/delete$'),
         lambda self, m, qs, uid: self.send(*redirect('/packing'))),
        (re.compile(r'^/packing/(\d+)/status$'),
         lambda self, m, qs, uid: self.send(*redirect(f'/packing/{m.group(1)}'))),
        (re.compile(r'^/packing/(\d+)$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pldetail(_id(m))))),

        # API — plain list endpoints are paginated to avoid an
        # unbounded SELECT * as the tables grow.
        (re.compile(r'^/api/items$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_list_items(qs)))),
        (re.compile(r'^/api/customers/search$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_customers_search(qs)))),
        (re.compile(r'^/api/customers$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_list_customers(qs)))),
        (re.compile(r'^/api/customers/(\d+)/items$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_customer_items(_id(m))))),
        (re.compile(r'^/api/categories$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_list_categories()))),
        (re.compile(r'^/api/categories/(\d+)/items$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_category_items(_id(m))))),
        (re.compile(r'^/api/items/filtered$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_items_filtered(qs)))),
        (re.compile(r'^/api/items/search$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_items_search(qs)))),
        (re.compile(r'^/api/proforma/(\d+)/items$'),
         lambda self, m, qs, uid: self.send(*json_response(self._api_proforma_items(_id(m))))),
        (re.compile(r'^/api/search$'),
         lambda self, m, qs, uid: self.send(*json_response(self.api_search(qs.get('q', [''])[0])))),
        (re.compile(r'^/api/quotations$'),
         lambda self, m, qs, uid: self.send(*json_response(self._quotations_fragment(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),
        (re.compile(r'^/api/proforma$'),
         lambda self, m, qs, uid: self.send(*json_response(self._proforma_fragment(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),
        (re.compile(r'^/api/packing$'),
         lambda self, m, qs, uid: self.send(*json_response(self._packing_fragment(
             q=qs.get('q', [''])[0], status=qs.get('status', [''])[0],
             page=qs.get('page', ['1'])[0], page_size=qs.get('page_size', [None])[0])))),

        # Business Metrics
        (re.compile(r'^/metrics$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_metrics(uid)))),

        # Scan & Import (OCR)
        (re.compile(r'^/ocr$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_ocr(uid)))),
        (re.compile(r'^/ocr/(\d+)$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_ocr_detail(uid, _id(m))))),
        (re.compile(r'^/ocr/(\d+)/file$'),
         lambda self, m, qs, uid: self.serve_ocr_file(_id(m))),
    ]

    # ── POST ─────────────────────────────────────────────────
    POST_ROUTES = [
        # Notifications (topbar bell read receipts)
        (re.compile(r'^/notifications/read$'),
         lambda self, m, form, uid: self.send(*self._notifications_mark_read(form, uid))),
        (re.compile(r'^/notifications/read-all$'),
         lambda self, m, form, uid: self.send(*self._notifications_mark_all_read(uid))),

        # Financial Year
        (re.compile(r'^/financial-year/select$'),
         lambda self, m, form, uid: self.handle_fy_select(form, uid)),
        (re.compile(r'^/financial-year/add$'),
         lambda self, m, form, uid: self.handle_fy_add(form, uid)),

        # Settings
        (re.compile(r'^/settings/profile$'),
         lambda self, m, form, uid: self.handle_profile_update(form, uid)),
        (re.compile(r'^/settings/users/(\d+)/toggle-status$'),
         lambda self, m, form, uid: self.send(*self._toggle_user_status(uid, _id(m)))),
        (re.compile(r'^/settings/users/(\d+)/delete$'),
         lambda self, m, form, uid: self.send(*self._delete_user(uid, _id(m)))),
        (re.compile(r'^/settings/restore$'),
         lambda self, m, form, uid: self.handle_settings_restore(form, uid)),
        (re.compile(r'^/settings/backup$'),
         lambda self, m, form, uid: self.handle_settings_backup_download(form, uid)),
        (re.compile(r'^/settings/backup/server$'),
         lambda self, m, form, uid: self.handle_settings_backup_server(form, uid)),
        (re.compile(r'^/settings/backup/server/([\w.\-]+)/delete$'),
         lambda self, m, form, uid: self.handle_settings_server_backup_delete(form, uid, m.group(1))
         if _is_safe_backup_filename(m.group(1)) else self.send(*html_response(self.pg_404(), 404))),

        # Customers
        (re.compile(r'^/customers/add$'),
         lambda self, m, form, uid: self.handle_customer_add(form)),
        (re.compile(r'^/api/customers/add$'),
         lambda self, m, form, uid: self.handle_customer_add_json(form)),
        (re.compile(r'^/customers/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_customer_edit(_id(m), form)),
        (re.compile(r'^/customers/(\d+)/items$'),
         lambda self, m, form, uid: self.handle_customer_items(_id(m), form)),
        (re.compile(r'^/customers/bulk-delete$'),
         lambda self, m, form, uid: self._delete_customers_bulk(form.get('ids', ''))),
        (re.compile(r'^/customers/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_customer(_id(m))),

        # Categories
        (re.compile(r'^/categories/add$'),
         lambda self, m, form, uid: self.handle_category_add(form)),
        (re.compile(r'^/categories/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_category_edit(_id(m), form)),
        (re.compile(r'^/categories/(\d+)/items$'),
         lambda self, m, form, uid: self.handle_category_items(_id(m), form)),
        (re.compile(r'^/categories/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_category(_id(m))),

        # Items
        (re.compile(r'^/items/add$'),
         lambda self, m, form, uid: self.handle_item_add(form)),
        (re.compile(r'^/api/items/add$'),
         lambda self, m, form, uid: self.handle_item_add_json(form)),
        (re.compile(r'^/items/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_item_edit(_id(m), form)),
        (re.compile(r'^/items/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_item(_id(m))),
        (re.compile(r'^/items/bulk-delete$'),
         lambda self, m, form, uid: self._delete_items_bulk(form.get('ids', ''))),

        # Quotations
        (re.compile(r'^/quotations/new$'),
         lambda self, m, form, uid: self.handle_quotation_create(form, uid)),
        (re.compile(r'^/quotations/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_quotation_edit(_id(m), form, uid)),
        (re.compile(r'^/quotations/(\d+)/status$'),
         lambda self, m, form, uid: self._set_status(
             'quotations', _id(m), form, '/quotations/{}')),
        (re.compile(r'^/quotations/(\d+)/duplicate$'),
         lambda self, m, form, uid: self.handle_quotation_duplicate(_id(m), uid)),
        (re.compile(r'^/quotations/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_quotation(_id(m))),

        # Proforma Invoices
        (re.compile(r'^/proforma/new$'),
         lambda self, m, form, uid: self.handle_proforma_create(form, uid)),
        (re.compile(r'^/proforma/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_proforma_edit(_id(m), form, uid)),
        (re.compile(r'^/proforma/(\d+)/status$'),
         lambda self, m, form, uid: self._set_status(
             'proforma_invoices', _id(m), form, '/proforma/{}')),
        (re.compile(r'^/proforma/(\d+)/duplicate$'),
         lambda self, m, form, uid: self.handle_proforma_duplicate(_id(m), uid)),
        (re.compile(r'^/proforma/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_proforma(_id(m))),

        # Packing Lists
        (re.compile(r'^/packing/new$'),
         lambda self, m, form, uid: self.handle_packing_create(form, uid)),
        (re.compile(r'^/packing/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_packing_edit(_id(m), form, uid)),
        (re.compile(r'^/packing/(\d+)/status$'),
         lambda self, m, form, uid: self._set_status(
             'packing_lists', _id(m), form, '/packing/{}')),
        (re.compile(r'^/packing/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_packing(_id(m))),

        # Salary
        (re.compile(r'^/salary/new$'),
         lambda self, m, form, uid: self.handle_employee_add(form)),
        (re.compile(r'^/salary/advance/bulk$'),
         lambda self, m, form, uid: self.handle_advance_bulk(form)),
        (re.compile(r'^/salary/(\d+)/attendance/clear$'),
         lambda self, m, form, uid: self.handle_attendance_clear(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/attendance/mark$'),
         lambda self, m, form, uid: self.handle_attendance_quick_mark(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/attendance$'),
         lambda self, m, form, uid: self.handle_attendance_save(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_employee_edit(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_employee(_id(m))),

        # Business Metrics
        (re.compile(r'^/metrics/refresh$'),
         lambda self, m, form, uid: self.handle_metrics_refresh(form, uid)),

        # Scan & Import (OCR)
        (re.compile(r'^/ocr/upload$'),
         lambda self, m, form, uid: self.handle_ocr_upload(form, uid)),
        (re.compile(r'^/ocr/(\d+)/save$'),
         lambda self, m, form, uid: self.handle_ocr_save(_id(m), form, uid)),
        (re.compile(r'^/ocr/(\d+)/quotation$'),
         lambda self, m, form, uid: self.handle_ocr_quotation(_id(m), form, uid)),
        (re.compile(r'^/ocr/(\d+)/delete$'),
         lambda self, m, form, uid: self.handle_ocr_delete(_id(m), uid)),
    ]

    # Administrative POST endpoints: single/bulk deletes of customers and
    # catalog items, plus salary actions (adding an employee, bulk salary
    # advances). Their handlers never even receive the session, so the
    # only place left to enforce "admin only" is here in the router —
    # mirroring the gate already applied to GET/POST /register above.
    ADMIN_ONLY_POST_PATTERNS = [
        re.compile(r'^/customers/(\d+)/delete$'),
        re.compile(r'^/customers/bulk-delete$'),
        re.compile(r'^/items/(\d+)/delete$'),
        re.compile(r'^/items/bulk-delete$'),
        re.compile(r'^/salary/(\d+)/delete$'),
        re.compile(r'^/salary/new$'),
        re.compile(r'^/salary/advance/bulk$'),
    ]

    # ── DISPATCH ─────────────────────────────────────────────
    # Public entry points: catch anything an inner handler raises so a
    # bug (a bad int()/float() conversion on attacker-influenced input,
    # a None where a row was expected, etc.) becomes a generic, logged
    # 500 instead of an unhandled exception. That matters more here than
    # it might elsewhere in a typical framework: this app's WSGI adapter
    # (passenger_wsgi.py) is the only thing standing between an
    # exception and the client, and without this it will happily hand
    # back a full Python traceback — file paths, query text, internals —
    # to whoever triggered it. Catching here means that never depends on
    # which entry point (socket server vs WSGI) is running.
    def do_GET(self):
        try:
            return self._do_GET()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self.send(_GENERIC_ERROR_PAGE, 500,
                              [('Content-Type', 'text/html; charset=utf-8')])

    def do_POST(self):
        try:
            return self._do_POST()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self.send(_GENERIC_ERROR_PAGE, 500,
                              [('Content-Type', 'text/html; charset=utf-8')])

    def _do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        sess = get_session(self.headers.get('Cookie'))
        user_id = sess.get('user_id')

        # Static files
        if path.startswith('/static/'):
            return self.serve_static(path)

        # Public routes
        if path == '/login' or path == '/':
            if path == '/' and user_id:
                return self.send(*redirect('/dashboard'))
            return self.send(*html_response(self.pg_login()))

        # Auth required below
        if not user_id:
            return self.send(*redirect('/login'))

        if path == '/logout':
            return self.handle_logout()

        # Creating accounts is an in-app, admin-only action — not a
        # public sign-up page. Anyone else hitting /register just bounces
        # to their dashboard.
        if path == '/register':
            if sess.get('role') != 'admin':
                return self.send(*redirect('/dashboard'))
            return self.send(*html_response(self.pg_register()))

        if path == '/change-password':
            return self.send(*html_response(self.pg_change_password()))

        for pattern, handler in self.GET_ROUTES:
            match = pattern.match(path)
            if match:
                return handler(self, match, qs, user_id)

        return self.send(*html_response(self.pg_404(), 404))

    def _do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            length = 0
        # A negative (or garbage) Content-Length is nonsensical, and passing
        # it straight to self.rfile.read() makes a plain socket file object
        # read until EOF instead of a bounded amount — treat it as no body.
        length = max(0, length)

        # Enforce a hard cap on body size for *every* POST route, before any
        # bytes are read and before auth/CSRF checks run. Without this, an
        # unauthenticated client could point a multi-gigabyte Content-Length
        # at /login (or any other route) and force the whole thing into
        # memory. /ocr/upload legitimately accepts larger file uploads, so
        # it gets its own, larger, endpoint-specific limit; every other
        # route is capped at MAX_REQUEST_BYTES.
        is_ocr_upload = path == '/ocr/upload'
        max_allowed = OCR_MAX_REQUEST_BYTES if is_ocr_upload else MAX_REQUEST_BYTES
        if length > max_allowed:
            # Refuse before reading: the body would otherwise be pulled fully
            # into memory just to be rejected.
            self.close_connection = True
            remaining = length
            if length <= OCR_MAX_DRAIN_BYTES:               # let the browser finish sending, so it
                while remaining > 0:                          # sees this message, not a reset connection
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            if is_ocr_upload:
                body_html = ('<h2 style="color:#DC2626">File too large</h2>'
                             '<p>That upload is over the size limit for Scan &amp; Import.</p>'
                             '<p><a href="/ocr">&larr; Back</a></p>')
            else:
                body_html = ('<h2 style="color:#DC2626">Request too large</h2>'
                             '<p>That request exceeds the maximum allowed size.</p>'
                             '<p><a href="/dashboard">&larr; Back to Dashboard</a></p>')
            return self.send(*html_response(
                '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Request too large</title></head>'
                '<body style="font-family:sans-serif;max-width:560px;margin:80px auto;text-align:center">'
                + body_html + '</body></html>', 413))
        raw_body = self.rfile.read(length)
        ct = self.headers.get('Content-Type', '')
        if 'multipart/form-data' in ct:
            # Binary uploads (e.g. a database backup) must be parsed from
            # the raw bytes — decoding the whole body as UTF-8 first (as
            # every other content type does below) would corrupt any
            # non-text file content before it's ever inspected.
            form = parse_multipart(raw_body, ct)
        else:
            body = raw_body.decode('utf-8', errors='replace')
            form = parse_form(body, ct)

        # Cross-site request forgery check — applies to every POST route,
        # including ones that don't (yet) carry their own per-form CSRF
        # token. See is_same_origin() for why this is safe to enforce
        # unconditionally. Body is read above first so the connection
        # stays healthy even when this rejects the request.
        if not is_same_origin(self.headers, path):
            return self.send(*html_response(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>Request blocked</title></head><body style="font-family:sans-serif;'
                'max-width:560px;margin:80px auto;text-align:center;color:#334155">'
                '<h2 style="color:#DC2626">Request blocked</h2>'
                '<p>This request could not be verified as coming from this site '
                '(Origin/Referer did not match) and was rejected. If you followed '
                'a link or bookmark to submit this, please go back and try again '
                'from within the app.</p>'
                '<p><a href="/dashboard">&larr; Back to Dashboard</a></p>'
                '</body></html>', 403))

        sess = get_session(self.headers.get('Cookie'))
        user_id = sess.get('user_id')

        if path == '/login':
            return self.handle_login(form)
        if not user_id:
            return self.send(*redirect('/login'))

        if path == '/register':
            if sess.get('role') != 'admin':
                return self.send(*redirect('/dashboard'))
            return self.handle_register(form)
        if path == '/change-password':
            return self.handle_change_password(form, user_id)

        if any(p.match(path) for p in self.ADMIN_ONLY_POST_PATTERNS) and sess.get('role') != 'admin':
            return self.send(*redirect('/dashboard'))

        for pattern, handler in self.POST_ROUTES:
            match = pattern.match(path)
            if match:
                return handler(self, match, form, user_id)

        return self.send(*html_response(self.pg_404(), 404))

    # ── API query helpers (paginated) ───────────────────────
    def _api_list_items(self, qs):
        limit, offset = _pagination(qs)
        db = get_db()
        try:
            return [dict(r) for r in db.execute(
                "SELECT * FROM items ORDER BY item_name LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()]
        finally:
            db.close()

    def _api_list_customers(self, qs):
        limit, offset = _pagination(qs)
        db = get_db()
        try:
            return [dict(r) for r in db.execute(
                "SELECT * FROM customers ORDER BY company_name LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()]
        finally:
            db.close()

    def _api_customers_search(self, qs):
        # Adapter for the Customer Management page's live search / sort /
        # status filter / pagination (see CustomerPagesMixin).
        def _first(key, default=''):
            return qs.get(key, [default])[0]
        return self._customers_fragment(
            q=_first('q'), page=_first('page', '1'), sort=_first('sort', 'oldest'),
            status=_first('status', 'all'), page_size=_first('page_size', '10'))

    def api_search(self, q):
        """Backs the global topbar search box (`/api/search?q=...`).

        Looks across quotations, customers, items, proforma invoices and
        packing lists for a loose match and returns a small, capped list
        per category so the dropdown stays fast and scannable. Customers
        and items don't have their own detail pages, so they link back to
        their list page pre-filtered by `?q=` (which those pages already
        know how to render) rather than a 404.
        """
        q = (q or '').strip()
        if len(q) < 2:
            return {'results': []}
        like = f'%{q}%'
        db = get_db()
        try:
            results = []
            for r in db.execute(
                "SELECT q.id, q.quotation_no, q.subject, c.company_name "
                "FROM quotations q LEFT JOIN customers c ON q.customer_id = c.id "
                "WHERE q.quotation_no LIKE ? OR q.subject LIKE ? OR c.company_name LIKE ? "
                "ORDER BY q.created_at DESC LIMIT 5", (like, like, like)).fetchall():
                subtitle = ' — '.join(p for p in (r['company_name'], r['subject']) if p)
                results.append({
                    'type': 'quotation', 'icon': 'bi-file-earmark-text',
                    'title': r['quotation_no'], 'subtitle': subtitle,
                    'url': f"/quotations/{r['id']}",
                })
            for r in db.execute(
                "SELECT id, company_name, contact_person FROM customers "
                "WHERE company_name LIKE ? OR contact_person LIKE ? "
                "ORDER BY company_name LIMIT 5", (like, like)).fetchall():
                results.append({
                    'type': 'customer', 'icon': 'bi-people',
                    'title': r['company_name'], 'subtitle': r['contact_person'] or '',
                    'url': '/customers?' + urllib.parse.urlencode({'q': r['company_name']}),
                })
            for r in db.execute(
                "SELECT id, item_name, description FROM items "
                "WHERE item_name LIKE ? OR description LIKE ? "
                "ORDER BY item_name LIMIT 5", (like, like)).fetchall():
                results.append({
                    'type': 'item', 'icon': 'bi-box-seam',
                    'title': r['item_name'], 'subtitle': r['description'] or '',
                    'url': '/items?' + urllib.parse.urlencode({'q': r['item_name']}),
                })
            for r in db.execute(
                "SELECT id, pi_no, subject FROM proforma_invoices "
                "WHERE pi_no LIKE ? OR subject LIKE ? "
                "ORDER BY created_at DESC LIMIT 5", (like, like)).fetchall():
                results.append({
                    'type': 'proforma', 'icon': 'bi-receipt',
                    'title': r['pi_no'], 'subtitle': r['subject'] or '',
                    'url': f"/proforma/{r['id']}",
                })
            for r in db.execute(
                "SELECT id, pl_no, subject FROM packing_lists "
                "WHERE pl_no LIKE ? OR subject LIKE ? "
                "ORDER BY created_at DESC LIMIT 5", (like, like)).fetchall():
                results.append({
                    'type': 'packing', 'icon': 'bi-box-seam',
                    'title': r['pl_no'], 'subtitle': r['subject'] or '',
                    'url': f"/packing/{r['id']}",
                })
            return {'results': results}
        finally:
            db.close()

    def _api_customer_items(self, cid):
        db = get_db()
        try:
            return [dict(r) for r in db.execute(
                "SELECT i.* FROM items i "
                "INNER JOIN customer_items ci ON i.id = ci.item_id "
                "WHERE ci.customer_id = ? ORDER BY i.item_name", (cid,)).fetchall()]
        finally:
            db.close()

    def _api_list_categories(self):
        db = get_db()
        try:
            return [dict(r) for r in db.execute(
                "SELECT * FROM categories ORDER BY name").fetchall()]
        finally:
            db.close()

    def _api_category_items(self, catid):
        db = get_db()
        try:
            return [dict(r) for r in db.execute(
                "SELECT i.* FROM items i "
                "INNER JOIN item_categories ic ON i.id = ic.item_id "
                "WHERE ic.category_id = ? ORDER BY i.item_name", (catid,)).fetchall()]
        finally:
            db.close()

    def _api_items_filtered(self, qs):
        # Filters by customer and/or category (either or both query
        # params may be omitted; when both are given, items must match
        # both to be included). Used by the quotation item picker so a
        # customer filter and a category filter can be combined instead
        # of one silently overriding the other.
        def _valid_id(raw):
            # A non-numeric value here used to blow up with an unhandled
            # ValueError (500). Treat anything that isn't a real ID as
            # "no filter" instead of erroring out.
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        customer_id = _valid_id(qs.get('customer_id', [None])[0])
        category_id = _valid_id(qs.get('category_id', [None])[0])
        limit, offset = _pagination(qs)
        sql = "SELECT DISTINCT i.* FROM items i WHERE 1=1"
        params = []
        if customer_id is not None:
            sql += " AND i.id IN (SELECT item_id FROM customer_items WHERE customer_id=?)"
            params.append(customer_id)
        if category_id is not None:
            sql += " AND i.id IN (SELECT item_id FROM item_categories WHERE category_id=?)"
            params.append(category_id)
        # Same pagination guard as every other list endpoint, so this can't
        # be used to pull the whole items table by simply omitting both
        # filter params.
        sql += " ORDER BY i.item_name LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        db = get_db()
        try:
            return [dict(r) for r in db.execute(sql, params).fetchall()]
        finally:
            db.close()

    def _api_items_search(self, qs):
        # Thin adapter: turns querystring params into the kwargs
        # `_items_fragment` (ItemPagesMixin) already expects, so the
        # item-catalog page's live search/sort/filter/pagination has a
        # real endpoint to call instead of 404ing.
        def _first(key, default=''):
            return qs.get(key, [default])[0]
        return self._items_fragment(
            q=_first('q'), page=_first('page', '1'),
            sort=_first('sort', 'name'), direction=_first('direction', 'asc'),
            type_filter=_first('type'), units=_first('units'),
            rate_min=_first('rate_min'), rate_max=_first('rate_max'),
            page_size=_first('page_size', '10'))

    def _api_proforma_items(self, pid):
        db = get_db()
        try:
            items = [dict(r) for r in db.execute(
                "SELECT pii.*, i.unit FROM proforma_invoice_items pii "
                "LEFT JOIN items i ON pii.item_id = i.id "
                "WHERE pii.proforma_id = ? ORDER BY pii.sort_order", (pid,)).fetchall()]
            pi = db.execute(
                "SELECT customer_id FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
            return {'items': items, 'customer_id': pi['customer_id'] if pi else None}
        finally:
            db.close()

    # ── POST action helpers (delete / status) ───────────────
    def _customer_in_use(self, db, cid):
        """Number of quotations / proforma invoices / packing lists that
        still point at this customer. Those rows carry a foreign key with
        no ON DELETE action, so such a customer can't be removed."""
        total = 0
        for table in ('quotations', 'proforma_invoices', 'packing_lists'):
            total += db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE customer_id=?", (cid,)).fetchone()['c']
        return total

    def _delete_customer(self, cid):
        return self._delete_customers_bulk(str(cid))

    @staticmethod
    def _parse_id_list(ids_raw):
        """Comma-separated ids -> list[int], ignoring anything unusable.

        `str.isdigit()` is True for characters int() rejects (e.g. '²'), and
        int() on a several-thousand-digit string is both slow and past
        Python's own conversion limit — either one turned a junk `ids`
        parameter into an unhandled ValueError and a 500. Bounding the
        length and catching the failure keeps a malformed request a no-op.
        """
        ids = []
        for part in str(ids_raw or '').split(','):
            part = part.strip()
            if not part.isdigit() or len(part) > 18:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                continue
        return ids

    def _delete_customers_bulk(self, ids_raw):
        ids = self._parse_id_list(ids_raw)
        deleted = blocked = 0
        if ids:
            db = get_db()
            try:
                # One IMMEDIATE transaction for the whole batch. The
                # "is this customer still referenced?" check and the DELETE
                # that follows it must not be separable: with a deferred
                # transaction another request could attach a quotation to
                # the customer in between, and since quotations.customer_id
                # is a foreign key with no ON DELETE action the DELETE would
                # then fail and take the entire batch down with it.
                begin_immediate(db)
                for cid in ids:
                    if self._customer_in_use(db, cid):
                        blocked += 1
                        continue
                    db.execute("DELETE FROM customer_items WHERE customer_id=?", (cid,))
                    db.execute("DELETE FROM customers WHERE id=?", (cid,))
                    deleted += 1
                db.commit()
            finally:
                db.close()
        return self.send(*redirect(f'/customers?deleted={deleted}&blocked={blocked}'))

    def _delete_category(self, cid):
        db = get_db()
        try:
            db.execute("DELETE FROM item_categories WHERE category_id=?", (cid,))
            db.execute("DELETE FROM categories WHERE id=?", (cid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/categories'))

    def _delete_item(self, iid):
        # Break all links pointing at this item so the catalog item
        # itself can be removed safely, even if it has been used in
        # quotations, proforma invoices, packing lists or is assigned
        # to customers.
        #
        # Customer and category assignment links are removed entirely.
        # Line items in quotes/proformas/packing lists keep their stored
        # snapshot (name/desc/rate) so historical documents are
        # preserved, but their pointer to the deleted catalog item is
        # nulled.
        db = get_db()
        try:
            db.execute("DELETE FROM customer_items WHERE item_id=?", (iid,))
            db.execute("DELETE FROM item_categories WHERE item_id=?", (iid,))
            db.execute("UPDATE quotation_items SET item_id=NULL WHERE item_id=?", (iid,))
            db.execute("UPDATE proforma_invoice_items SET item_id=NULL WHERE item_id=?", (iid,))
            db.execute("UPDATE packing_list_items SET item_id=NULL WHERE item_id=?", (iid,))
            db.execute("DELETE FROM items WHERE id=?", (iid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/items'))

    def _delete_items_bulk(self, ids_raw):
        ids = self._parse_id_list(ids_raw)
        if ids:
            db = get_db()
            try:
                placeholders = ','.join('?' * len(ids))
                db.execute(f"DELETE FROM customer_items WHERE item_id IN ({placeholders})", ids)
                db.execute(f"DELETE FROM item_categories WHERE item_id IN ({placeholders})", ids)
                db.execute(f"UPDATE quotation_items SET item_id=NULL WHERE item_id IN ({placeholders})", ids)
                db.execute(f"UPDATE proforma_invoice_items SET item_id=NULL WHERE item_id IN ({placeholders})", ids)
                db.execute(f"UPDATE packing_list_items SET item_id=NULL WHERE item_id IN ({placeholders})", ids)
                db.execute(f"DELETE FROM items WHERE id IN ({placeholders})", ids)
                db.commit()
            finally:
                db.close()
        return self.send(*redirect('/items'))

    def _delete_quotation(self, qid):
        db = get_db()
        try:
            db.execute("DELETE FROM quotation_items WHERE quotation_id=?", (qid,))
            db.execute("DELETE FROM quotations WHERE id=?", (qid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/quotations'))

    def _delete_proforma(self, pid):
        # A packing list generated from this proforma is a real, historical
        # record in its own right (goods already shipped) — it shouldn't be
        # deleted or blocked, just unlinked from the proforma it came from,
        # same idea as how _delete_item nulls out pointers instead of
        # cascading.
        db = get_db()
        try:
            # `packing_lists.pi_id`, not `proforma_id` — see the schema in
            # database.py. The wrong column name made every proforma delete
            # raise "no such column" (a 500) and delete nothing.
            begin_immediate(db)
            db.execute("UPDATE packing_lists SET pi_id=NULL WHERE pi_id=?", (pid,))
            db.execute("DELETE FROM proforma_invoice_items WHERE proforma_id=?", (pid,))
            db.execute("DELETE FROM proforma_invoices WHERE id=?", (pid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/proforma'))

    def _delete_packing(self, plid):
        db = get_db()
        try:
            db.execute(
                "DELETE FROM packing_list_bags WHERE packing_list_item_id IN "
                "(SELECT id FROM packing_list_items WHERE packing_list_id=?)", (plid,))
            db.execute("DELETE FROM packing_list_items WHERE packing_list_id=?", (plid,))
            db.execute("DELETE FROM packing_lists WHERE id=?", (plid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/packing'))

    def _delete_employee(self, eid):
        db = get_db()
        try:
            # There is no `salary_advances` table in this schema (see
            # database.py init_db) — deleting from it raised "no such
            # table" and made every employee delete a 500 that removed
            # nothing. Attendance rows are linked ON DELETE CASCADE, but
            # are cleared explicitly too so the delete doesn't depend on
            # the foreign_keys pragma being honoured.
            begin_immediate(db)
            db.execute("DELETE FROM attendance WHERE employee_id=?", (eid,))
            db.execute("DELETE FROM employees WHERE id=?", (eid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/salary'))

    # Which status list applies to which table. The values themselves are
    # NOT restated here: each page module already owns the single list its
    # create/edit forms and filter chips are built from, so referencing it
    # through `self` means the two can't drift apart. A duplicated copy did
    # exactly that — it listed Accepted/Rejected/Expired for quotations and
    # Packed/Shipped/Delivered for packing lists, none of which this app
    # offers, so every legitimate status change fell through to the
    # fallback and was silently rewritten to 'Draft'.
    _STATUS_LIST_ATTR = {
        'quotations': 'QUOTATION_STATUSES',
        'proforma_invoices': 'PROFORMA_STATUSES',
        'packing_lists': 'PACKING_STATUSES',
    }

    def _set_status(self, table, row_id, form, redirect_fmt):
        attr = self._STATUS_LIST_ATTR.get(table)
        if attr is None:
            # `table` is interpolated into the SQL below, so it must never
            # be anything other than one of the three known keys.
            return self.send(*redirect(redirect_fmt.format(row_id)))
        allowed = getattr(self, attr)
        status = (form.get('status') or 'Draft').strip()
        if status not in allowed:
            status = 'Draft'
        db = get_db()
        try:
            db.execute(f"UPDATE {table} SET status=? WHERE id=?", (status, row_id))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect(redirect_fmt.format(row_id)))
