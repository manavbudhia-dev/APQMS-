import re
import urllib.parse
from http.cookies import SimpleCookie

from utils.http_utils import redirect
from utils.http_utils import json_response
from utils.http_utils import html_response
from utils.http_utils import get_session
from utils.http_utils import sessions
from utils.http_utils import parse_form
from database import get_db


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


class RoutingMixin:
    # ── GET ──────────────────────────────────────────────────
    GET_ROUTES = [
        (re.compile(r'^/dashboard$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_dashboard(uid)))),

        # Customers
        (re.compile(r'^/customers$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_customers()))),
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

        # Salary
        (re.compile(r'^/salary$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_list(qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/new$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_salary_new()))),
        (re.compile(r'^/salary/(\d+)/attendance$'),
         lambda self, m, qs, uid: self.send(*html_response(
             self.pg_salary_attendance(_id(m), qs.get('month', [None])[0])))),
        (re.compile(r'^/salary/(\d+)/edit$'),
         lambda self, m, qs, uid: self.send(*html_response(self.pg_salary_edit(_id(m))))),
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
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pilist()))),
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
         lambda self, m, qs, uid: self.send(*html_response(self.pg_pllist()))),
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
    ]

    # ── POST ─────────────────────────────────────────────────
    POST_ROUTES = [
        # Customers
        (re.compile(r'^/customers/add$'),
         lambda self, m, form, uid: self.handle_customer_add(form)),
        (re.compile(r'^/api/customers/add$'),
         lambda self, m, form, uid: self.handle_customer_add_json(form)),
        (re.compile(r'^/customers/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_customer_edit(_id(m), form)),
        (re.compile(r'^/customers/(\d+)/items$'),
         lambda self, m, form, uid: self.handle_customer_items(_id(m), form)),
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
        (re.compile(r'^/salary/(\d+)/attendance/clear$'),
         lambda self, m, form, uid: self.handle_attendance_clear(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/attendance$'),
         lambda self, m, form, uid: self.handle_attendance_save(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/edit$'),
         lambda self, m, form, uid: self.handle_employee_edit(_id(m), form)),
        (re.compile(r'^/salary/(\d+)/delete$'),
         lambda self, m, form, uid: self._delete_employee(_id(m))),
    ]

    # ── DISPATCH ─────────────────────────────────────────────
    def do_GET(self):
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

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8', errors='replace')
        ct = self.headers.get('Content-Type', '')
        form = parse_form(body, ct)
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
        customer_id = qs.get('customer_id', [None])[0]
        category_id = qs.get('category_id', [None])[0]
        sql = "SELECT DISTINCT i.* FROM items i WHERE 1=1"
        params = []
        if customer_id:
            sql += " AND i.id IN (SELECT item_id FROM customer_items WHERE customer_id=?)"
            params.append(int(customer_id))
        if category_id:
            sql += " AND i.id IN (SELECT item_id FROM item_categories WHERE category_id=?)"
            params.append(int(category_id))
        sql += " ORDER BY i.item_name"
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
    def _delete_customer(self, cid):
        db = get_db()
        try:
            db.execute("DELETE FROM customers WHERE id=?", (cid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/customers'))

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
        ids = [int(x) for x in str(ids_raw).split(',') if x.strip().isdigit()]
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
        db = get_db()
        try:
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
            db.execute("DELETE FROM attendance WHERE employee_id=?", (eid,))
            db.execute("DELETE FROM employees WHERE id=?", (eid,))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect('/salary'))

    def _set_status(self, table, row_id, form, redirect_fmt):
        status = form.get('status', 'Draft')
        db = get_db()
        try:
            db.execute(f"UPDATE {table} SET status=? WHERE id=?", (status, row_id))
            db.commit()
        finally:
            db.close()
        return self.send(*redirect(redirect_fmt.format(row_id)))
