import json
import math
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import json_response
from database import get_db


def _clean_address(form):
    return form.get('address', '').replace('\r\n', '\n').replace('\r', '\n').strip()


def _is_active_from_form(form):
    """The add/edit modals send status=active|inactive; a missing field
    (e.g. the quick-add popup on the quotation form) means Active."""
    return 0 if str(form.get('status', 'active')).strip().lower() == 'inactive' else 1


def _dash(value):
    """Value for display, or a muted dash when empty."""
    value = (value or '').strip() if isinstance(value, str) else value
    return esc(value) if value else '<span class="cm-muted">-</span>'


class CustomerPagesMixin:
    CUSTOMERS_PAGE_SIZE = 10
    CUSTOMERS_PAGE_SIZES = (10, 20, 50)
    # key -> (ORDER BY clause, label shown in the "Sort by" dropdown)
    CUSTOMERS_SORTS = {
        'oldest': ("created_at ASC, id ASC", 'Oldest first'),
        'newest': ("created_at DESC, id DESC", 'Newest first'),
        'company_asc': ("LOWER(company_name) ASC", 'Company (A\u2013Z)'),
        'company_desc': ("LOWER(company_name) DESC", 'Company (Z\u2013A)'),
        'contact_asc': ("(contact_person IS NULL OR contact_person = '') ASC, "
                        "LOWER(contact_person) ASC", 'Contact person (A\u2013Z)'),
        'contact_desc': ("(contact_person IS NULL OR contact_person = '') ASC, "
                         "LOWER(contact_person) DESC", 'Contact person (Z\u2013A)'),
    }
    CUSTOMERS_STATUS_FILTERS = (
        ('all', 'All Customers'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    )

    # ── FORM HANDLERS ────────────────────────────────────────
    def handle_customer_add(self, form):
        db = get_db()
        db.execute(
            "INSERT INTO customers "
            "(contact_person,company_name,address,phone,email,gstin,is_active) "
            "VALUES (?,?,?,?,?,?,?)",
            (form.get('contact_person', '').strip(), form.get('company_name', '').strip(),
             _clean_address(form), form.get('phone', '').strip(),
             form.get('email', '').strip(), form.get('gstin', '').strip(),
             _is_active_from_form(form)))
        db.commit()
        db.close()
        return self.send(*redirect('/customers'))


    def handle_customer_add_json(self, form):
        """AJAX twin of handle_customer_add: used by the inline "+ Add
        New Customer" modal on the quotation form so a customer can be
        created without losing whatever the user was already filling
        in. Returns the new row as JSON instead of redirecting."""
        contact_person = form.get('contact_person', '').strip()
        company_name = form.get('company_name', '').strip()
        if not company_name:
            return self.send(*json_response(
                {'error': 'Company name is required.'}, 400))
        address = form.get('address', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db = get_db()
        cur = db.execute(
            "INSERT INTO customers "
            "(contact_person,company_name,address,phone,email,gstin,is_active) "
            "VALUES (?,?,?,?,?,?,?)",
            (contact_person, company_name, address, form.get('phone', ''),
             form.get('email', ''), form.get('gstin', ''), _is_active_from_form(form)))
        new_id = cur.lastrowid
        db.commit()
        db.close()
        return self.send(*json_response({
            'id': new_id,
            'contact_person': contact_person,
            'company_name': company_name,
            'address': address,
        }))


    def handle_customer_edit(self, cid, form):
        db = get_db()
        db.execute(
            "UPDATE customers SET contact_person=?,company_name=?,address=?,"
            "phone=?,email=?,gstin=?,is_active=? WHERE id=?",
            (form.get('contact_person', '').strip(), form.get('company_name', '').strip(),
             _clean_address(form), form.get('phone', '').strip(),
             form.get('email', '').strip(), form.get('gstin', '').strip(),
             _is_active_from_form(form), cid))
        db.commit()
        db.close()
        return self.send(*redirect('/customers'))


    def handle_customer_items(self, cid, form):
        db = get_db()
        # Clear existing assignments
        db.execute("DELETE FROM customer_items WHERE customer_id=?", (cid,))
        # Add new assignments
        item_ids = form.getlist('item_ids[]')
        for iid in item_ids:
            try:
                iid = int(iid)
                db.execute("INSERT OR IGNORE INTO customer_items (customer_id, item_id) VALUES (?,?)", (cid, iid))
            except (TypeError, ValueError):
                pass
        db.commit()
        db.close()
        return self.send(*redirect('/customers'))


    # ── PAGE: CUSTOMERS ──────────────────────────────────────
    @staticmethod
    def _customer_form_fields(c=None):
        """Shared field set for the Add and Edit modals."""
        def v(key):
            return esc((c[key] if c is not None else '') or '')
        active = (c is None) or (c['is_active'] is None) or bool(c['is_active'])
        placeholder = ('' if c is not None else
                       ' placeholder="e.g. STREET NO.1, ANAND NAGAR&#10;NEAR KIDZ NEST SCHOOL, MAKSUDAN&#10;'
                       'JALANDHAR (PUNJAB), INDIA&#10;PIN CODE - 144008"')
        return f'''
        <div class="row">
          <div class="col-md-6 mb-2"><label class="form-label">Company Name <span class="text-danger">*</span></label>
            <input name="company_name" class="form-control" value="{v("company_name")}" required></div>
          <div class="col-md-6 mb-2"><label class="form-label">Contact Person</label>
            <input name="contact_person" class="form-control" value="{v("contact_person")}"></div>
        </div>
        <div class="mb-2"><label class="form-label">Address</label>
          <textarea name="address" class="form-control" rows="4"{placeholder}>{v("address")}</textarea></div>
        <div class="row">
          <div class="col-md-6 mb-2"><label class="form-label">Phone</label>
            <input name="phone" class="form-control" value="{v("phone")}"></div>
          <div class="col-md-6 mb-2"><label class="form-label">Email</label>
            <input name="email" type="email" class="form-control" value="{v("email")}"></div>
        </div>
        <div class="row">
          <div class="col-md-6 mb-2"><label class="form-label">GSTIN</label>
            <input name="gstin" class="form-control" value="{v("gstin")}"></div>
          <div class="col-md-6 mb-2"><label class="form-label">Status</label>
            <select name="status" class="form-select">
              <option value="active"{" selected" if active else ""}>Active</option>
              <option value="inactive"{"" if active else " selected"}>Inactive</option>
            </select></div>
        </div>'''

    def _customers_rows_and_modals_html(self, rows_data, start_num=1):
        rows = ''
        modals = ''
        for offset, c in enumerate(rows_data):
            cid = c["id"]
            active = (c["is_active"] is None) or bool(c["is_active"])
            name = (c["contact_person"] or '').strip()
            email = (c["email"] or '').strip()
            company = esc(c["company_name"])
            em_dash, en_dash = '\u2014', '\u2013'
            dash_html = "<span class='cm-muted'>-</span>"
            status_badge = ('<span class="cm-status cm-status-active">Active</span>' if active else
                            '<span class="cm-status cm-status-inactive">Inactive</span>')
            rows += (
                f'<tr>'
                f'<td class="text-center"><input type="checkbox" class="cm-row-check form-check-input" value="{cid}" aria-label="Select {company}"></td>'
                f'<td class="cm-muted">{start_num + offset}</td>'
                f'<td><div class="cm-contact-name">{esc(name) if name else em_dash}</div>'
                f'<div class="cm-contact-sub">{esc(email) if email else en_dash}</div></td>'
                f'<td class="cm-company">{company}</td>'
                f'<td class="cm-address">{esc_nl(c["address"]) if c["address"] else dash_html}</td>'
                f'<td>{_dash(c["phone"])}</td>'
                f'<td class="cm-gstin">{_dash(c["gstin"])}</td>'
                f'<td>{status_badge}</td>'
                f'<td class="text-end cm-actions">'
                f'<button type="button" class="cm-icon-btn" data-bs-toggle="modal" data-bs-target="#view{cid}" title="View" aria-label="View {company}"><i class="bi bi-eye"></i></button>'
                f'<button type="button" class="cm-icon-btn" data-bs-toggle="modal" data-bs-target="#edit{cid}" title="Edit" aria-label="Edit {company}"><i class="bi bi-pencil"></i></button>'
                f'<button type="button" class="cm-icon-btn cm-icon-btn-danger" data-bs-toggle="modal" data-bs-target="#del{cid}" title="Delete" aria-label="Delete {company}"><i class="bi bi-trash"></i></button>'
                f'</td></tr>')

            def detail(label, value_html):
                return (f'<div class="cm-detail"><div class="cm-detail-label">{label}</div>'
                        f'<div class="cm-detail-value">{value_html}</div></div>')

            modals += f'''
<div class="modal fade" id="view{cid}" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered"><div class="modal-content">
    <div class="modal-header"><h5 class="modal-title">Customer Details</h5>
      <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
    <div class="modal-body">
      <div class="d-flex align-items-start justify-content-between gap-2 mb-3">
        <div><div class="cm-view-company">{company}</div>
          <div class="cm-contact-sub">{esc(name) if name else "No contact person"}</div></div>
        {status_badge}
      </div>
      {detail("Address", esc_nl(c["address"]) if c["address"] else "-")}
      <div class="row">
        <div class="col-6">{detail("Phone", esc(c["phone"]) if c["phone"] else "-")}</div>
        <div class="col-6">{detail("Email", esc(email) if email else "-")}</div>
      </div>
      {detail("GSTIN", esc(c["gstin"]) if c["gstin"] else "-")}
    </div>
    <div class="modal-footer">
      <a href="/customers/{cid}/products" class="btn btn-outline-secondary me-auto" title="Optional reference list — not required to create quotations"><i class="bi bi-box-seam"></i> Usual Products (optional)</a>
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
      <button type="button" class="btn btn-primary" onclick="cmSwitchModal('view{cid}','edit{cid}')"><i class="bi bi-pencil"></i> Edit</button>
    </div>
  </div></div>
</div>
<div class="modal fade" id="edit{cid}" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/customers/{cid}/edit">
      <div class="modal-header"><h5 class="modal-title">Edit Customer</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">{self._customer_form_fields(c)}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-lg"></i> Save</button>
      </div>
    </form>
  </div></div>
</div>
<div class="modal fade" id="del{cid}" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content">
    <div class="modal-body">
      <h5 class="cm-del-title">Delete customer?</h5>
      <p class="cm-del-text">This removes <strong>{company}</strong> from your customers. Customers already used in quotations, proforma invoices or packing lists can't be deleted &mdash; mark them Inactive instead.</p>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <form method="POST" action="/customers/{cid}/delete" class="d-inline">
        <button type="submit" class="btn btn-danger">Delete</button>
      </form>
    </div>
  </div></div>
</div>'''
        return rows, modals

    def _customers_empty_state(self, has_any, q, filtered):
        """Message for zero results, whether the table is truly empty or
        just has no matches for the current search / status filter."""
        if not has_any:
            return ('<div class="cm-empty">'
                    '<div class="cm-empty-icon"><i class="bi bi-people"></i></div>'
                    '<h6>No customers yet</h6>'
                    '<p class="text-muted">Once you add a customer, it will show up here.</p>'
                    '<button type="button" class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addCust">'
                    '<i class="bi bi-plus-circle"></i> Add your first customer</button>'
                    '</div>')
        detail = f' matching &ldquo;{esc(q)}&rdquo;' if q else ''
        return ('<div class="cm-empty">'
                '<div class="cm-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching customers</h6>'
                f'<p class="text-muted">No customers{detail} for the current filters.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="cmClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _customers_pagination_html(self, page, total_pages):
        def btn(p, label, disabled=False, active=False, aria=''):
            cls = 'cm-page-btn' + (' active' if active else '')
            dis = ' disabled' if disabled else ''
            aria_attr = f' aria-label="{aria}"' if aria else ''
            return f'<button type="button" class="{cls}"{dis}{aria_attr} onclick="cmGoToPage({p})">{label}</button>'

        html = btn(page - 1, '<i class="bi bi-chevron-left"></i>', disabled=(page <= 1), aria='Previous page')
        start = max(1, page - 2)
        end = min(total_pages, start + 4)
        start = max(1, end - 4)
        if start > 1:
            html += btn(1, '1')
            if start > 2:
                html += '<span class="cm-page-gap">&hellip;</span>'
        for p in range(start, end + 1):
            html += btn(p, str(p), active=(p == page))
        if end < total_pages:
            if end < total_pages - 1:
                html += '<span class="cm-page-gap">&hellip;</span>'
            html += btn(total_pages, str(total_pages))
        html += btn(page + 1, '<i class="bi bi-chevron-right"></i>', disabled=(page >= total_pages), aria='Next page')
        return html

    def _customers_fragment(self, q='', page='1', sort='oldest', status='all', page_size='10'):
        """Fetch, filter, sort and paginate customers; return rendered
        HTML fragments used both for the initial page load and the JSON
        API (/api/customers/search) that powers live search / sort /
        filter / pagination without a full reload."""
        q = (q or '').strip()
        if sort not in self.CUSTOMERS_SORTS:
            sort = 'oldest'
        if status not in dict(self.CUSTOMERS_STATUS_FILTERS):
            status = 'all'
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = self.CUSTOMERS_PAGE_SIZE
        if page_size not in self.CUSTOMERS_PAGE_SIZES:
            page_size = self.CUSTOMERS_PAGE_SIZE

        where, params = [], []
        if q:
            like = f"%{q}%"
            where.append("(contact_person LIKE ? OR company_name LIKE ? OR address LIKE ? "
                         "OR phone LIKE ? OR email LIKE ? OR gstin LIKE ?)")
            params.extend([like] * 6)
        if status == 'active':
            where.append("COALESCE(is_active, 1) = 1")
        elif status == 'inactive':
            where.append("COALESCE(is_active, 1) = 0")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        db = get_db()
        try:
            counts = db.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN COALESCE(is_active,1)=1 THEN 1 ELSE 0 END) active "
                "FROM customers").fetchone()
            total_all = counts['total'] or 0
            active_all = counts['active'] or 0
            total = db.execute(f"SELECT COUNT(*) c FROM customers{where_sql}", params).fetchone()['c']
            total_pages = max(1, math.ceil(total / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            rows_data = db.execute(
                f"SELECT * FROM customers{where_sql} ORDER BY {self.CUSTOMERS_SORTS[sort][0]} "
                f"LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
        finally:
            db.close()

        if rows_data:
            rows_html, modals_html = self._customers_rows_and_modals_html(rows_data, start_num=offset + 1)
        else:
            empty = self._customers_empty_state(total_all > 0, q, bool(where))
            rows_html = f'<tr class="cm-empty-row"><td colspan="9" class="p-0">{empty}</td></tr>'
            modals_html = ''

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + page_size, total)
        noun = 'customer' if total == 1 else 'customers'
        count_label = (f'Showing {range_start} &ndash; {range_end} of {total} {noun}'
                       if total else 'No customers found')

        return {
            'rows_html': rows_html,
            'modals_html': modals_html,
            'pagination_html': self._customers_pagination_html(page, total_pages),
            'count_label': count_label,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'stats': {'total': total_all, 'active': active_all,
                      'inactive': total_all - active_all},
        }

    def pg_customers(self, q='', page='1', sort='oldest', status='all',
                     page_size='10', deleted='0', blocked='0'):
        if sort not in self.CUSTOMERS_SORTS:
            sort = 'oldest'
        if status not in dict(self.CUSTOMERS_STATUS_FILTERS):
            status = 'all'
        frag = self._customers_fragment(q=q, page=page, sort=sort, status=status, page_size=page_size)
        stats = frag['stats']

        # One-off notice after a delete (see _delete_customers_bulk).
        try:
            deleted_n, blocked_n = max(0, int(deleted)), max(0, int(blocked))
        except (TypeError, ValueError):
            deleted_n = blocked_n = 0
        notice_html = ''
        if deleted_n:
            notice_html += (f'<div class="alert alert-success alert-dismissible fade show py-2" role="alert">'
                            f'{deleted_n} customer{"s" if deleted_n != 1 else ""} deleted.'
                            f'<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>')
        if blocked_n:
            notice_html += (f'<div class="alert alert-warning alert-dismissible fade show py-2" role="alert">'
                            f'{blocked_n} customer{"s" if blocked_n != 1 else ""} could not be deleted because '
                            f'{"they are" if blocked_n != 1 else "it is"} used in quotations, proforma invoices or packing lists. '
                            f'Mark {"them" if blocked_n != 1 else "it"} Inactive instead.'
                            f'<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>')

        filter_items = ''.join(
            f'<li><button type="button" class="dropdown-item cm-dd-item{" active" if k == status else ""}" '
            f'data-value="{k}" onclick="cmSetStatus(\'{k}\')">{esc(label)}</button></li>'
            for k, label in self.CUSTOMERS_STATUS_FILTERS)
        filter_label = dict(self.CUSTOMERS_STATUS_FILTERS)[status]
        sort_items = ''.join(
            f'<li><button type="button" class="dropdown-item cm-dd-item{" active" if k == sort else ""}" '
            f'data-value="{k}" onclick="cmSetSort(\'{k}\')">{esc(label)}</button></li>'
            for k, (_, label) in self.CUSTOMERS_SORTS.items())
        sort_label = 'Sort by' if sort == 'oldest' else self.CUSTOMERS_SORTS[sort][1]
        page_size_options = ''.join(
            f'<option value="{sz}"{" selected" if sz == frag["page_size"] else ""}>{sz} / page</option>'
            for sz in self.CUSTOMERS_PAGE_SIZES)

        state = {'q': q.strip(), 'page': frag['page'], 'sort': sort, 'status': status,
                 'pageSize': frag['page_size'],
                 'sortLabels': {k: v[1] for k, v in self.CUSTOMERS_SORTS.items()},
                 'statusLabels': dict(self.CUSTOMERS_STATUS_FILTERS)}
        script = CUSTOMERS_PAGE_JS.replace('__STATE__', json.dumps(state).replace('</', '<\\/'))

        content = f'''
<nav class="cm-breadcrumb" aria-label="Breadcrumb">
  <a href="/dashboard"><i class="bi bi-house-door"></i> Home</a>
  <i class="bi bi-chevron-right"></i>
  <span>Customers</span>
</nav>

{notice_html}

<div class="cm-header">
  <div class="d-flex align-items-center gap-3">
    <div class="cm-icon-box"><i class="bi bi-people"></i></div>
    <div>
      <h4 class="cm-title">Customer Management</h4>
      <div class="cm-subtitle">Manage your customers, contact details and GST information.</div>
    </div>
  </div>
  <button class="btn btn-primary cm-add-btn" data-bs-toggle="modal" data-bs-target="#addCust">
    <i class="bi bi-plus-lg"></i> Add Customer</button>
</div>

<div class="row g-3 mb-4">
  <div class="col-lg-4 col-md-6">
    <div class="cm-stat"><div class="cm-stat-icon" style="background:#E0EDFF;color:#2563EB"><i class="bi bi-people"></i></div>
      <div><div class="cm-stat-value" id="cmStatTotal">{stats["total"]}</div><div class="cm-stat-label">Total Customers</div></div></div>
  </div>
  <div class="col-lg-4 col-md-6">
    <div class="cm-stat"><div class="cm-stat-icon" style="background:#DCFCE7;color:#16A34A"><i class="bi bi-building"></i></div>
      <div><div class="cm-stat-value" id="cmStatActive">{stats["active"]}</div><div class="cm-stat-label">Active Customers</div></div></div>
  </div>
  <div class="col-lg-4 col-md-12">
    <div class="cm-stat"><div class="cm-stat-icon" style="background:#FEE2E2;color:#DC2626"><i class="bi bi-person-x"></i></div>
      <div><div class="cm-stat-value" id="cmStatInactive">{stats["inactive"]}</div><div class="cm-stat-label">Inactive Customers</div></div></div>
  </div>
</div>

<div class="table-card cm-card">
  <div class="cm-toolbar">
    <div class="cm-search">
      <i class="bi bi-search"></i>
      <input type="text" id="custSearchInput" placeholder="Search customers by name, company, city..." autocomplete="off" value="{esc(q)}">
      <button type="button" id="cmSearchClear" class="cm-search-clear d-none" onclick="cmClearSearch()" aria-label="Clear search"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="d-flex align-items-center gap-2 flex-wrap">
      <div class="dropdown">
        <button type="button" class="cm-select-btn" data-bs-toggle="dropdown" aria-expanded="false">
          <i class="bi bi-funnel"></i><span id="cmFilterLabel">{esc(filter_label)}</span><i class="bi bi-chevron-down cm-caret"></i></button>
        <ul class="dropdown-menu dropdown-menu-end cm-dd-menu" id="cmFilterMenu">{filter_items}</ul>
      </div>
      <div class="dropdown">
        <button type="button" class="cm-select-btn" data-bs-toggle="dropdown" aria-expanded="false">
          <i class="bi bi-arrow-down-up"></i><span id="cmSortLabel">{esc(sort_label)}</span><i class="bi bi-chevron-down cm-caret"></i></button>
        <ul class="dropdown-menu dropdown-menu-end cm-dd-menu" id="cmSortMenu">{sort_items}</ul>
      </div>
    </div>
  </div>

  <div class="cm-bulk d-none" id="cmBulkBar">
    <span><strong id="cmBulkCount">0</strong> selected</span>
    <div class="d-flex gap-2">
      <button type="button" class="btn btn-sm btn-outline-secondary" onclick="cmClearSelection()">Clear</button>
      <button type="button" class="btn btn-sm btn-outline-danger" data-bs-toggle="modal" data-bs-target="#cmBulkDeleteModal"><i class="bi bi-trash"></i> Delete Selected</button>
    </div>
  </div>

  <div class="table-responsive-wrapper">
    <table class="table mb-0 cm-table">
      <thead>
        <tr>
          <th style="width:44px" class="text-center"><input type="checkbox" class="form-check-input" id="cmSelectAll" aria-label="Select all customers"></th>
          <th style="width:44px">#</th>
          <th>Contact Person</th>
          <th>Company</th>
          <th>Address</th>
          <th>Phone</th>
          <th>GSTIN</th>
          <th>Status</th>
          <th class="text-end">Actions</th>
        </tr>
      </thead>
      <tbody id="customersBody">{frag["rows_html"]}</tbody>
    </table>
  </div>

  <div class="cm-footer">
    <span class="cm-count" id="custCountLabel">{frag["count_label"]}</span>
    <div class="d-flex align-items-center gap-2 flex-wrap">
      <div class="cm-pager" id="custPagination">{frag["pagination_html"]}</div>
      <select id="cmPageSize" class="form-select form-select-sm cm-page-size">{page_size_options}</select>
    </div>
  </div>
</div>

<div class="cm-tip"><i class="bi bi-info-circle-fill"></i>
  <span><strong>Tip:</strong> Keep customer information up to date for accurate quotations and invoices.</span></div>

<div id="customersModals">{frag["modals_html"]}</div>

<div class="modal fade" id="cmBulkDeleteModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content">
    <div class="modal-body">
      <h5 class="cm-del-title">Delete <span id="cmBulkDeleteCount">0</span> customers?</h5>
      <p class="cm-del-text">This removes the selected customers. Any that are already used in quotations, proforma invoices or packing lists will be kept &mdash; mark those Inactive instead.</p>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <form method="POST" action="/customers/bulk-delete" class="d-inline">
        <input type="hidden" name="ids" id="cmBulkDeleteIds" value="">
        <button type="submit" class="btn btn-danger">Delete</button>
      </form>
    </div>
  </div></div>
</div>

<div class="modal fade" id="addCust" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/customers/add">
      <div class="modal-header"><h5 class="modal-title">Add Customer</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">{self._customer_form_fields()}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-plus-lg"></i> Add</button>
      </div>
    </form>
  </div></div>
</div>
<script>{script}</script>'''
        return self._layout('Customers', 'Customer Management', content, 'customers')


    # ── PAGE: CUSTOMER PRODUCTS ───────────────────────────────
    def pg_customer_products(self, cid):
        db = get_db()
        customer = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not customer:
            db.close()
            return self.pg_404()
        all_items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        assigned = [r['item_id'] for r in db.execute(
            "SELECT item_id FROM customer_items WHERE customer_id=?", (cid,)).fetchall()]
        db.close()

        rows = ''
        for i in all_items:
            checked = 'checked' if i['id'] in assigned else ''
            rows += (f'<tr><td><input type="checkbox" name="item_ids[]" value="{i["id"]}" {checked} class="form-check-input"></td>'
                     f'<td><strong>{esc(i["item_name"])}</strong></td>'
                     f'<td style="white-space:pre-line">{esc_nl(i["description"] or "-")}</td>'
                     f'<td>{esc(i["unit"])}</td>'
                     f'<td><strong style="color:#DC2626">Rs. {fmt_rupee(i["rate_per_unit"])}</strong></td></tr>')

        content = f'''
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-box-seam"></i> Usual Products for {esc(customer["company_name"])}</h5>
  <a href="/customers" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
</div>
<div class="alert alert-info py-2 mb-3"><i class="bi bi-info-circle"></i> This is just a personal reference list of what this customer usually orders &mdash; it is <strong>not required</strong> to create a quotation. When making a quotation you can pick any item from the full catalog regardless of what's checked here.</div>
<form method="POST" action="/customers/{cid}/items">
<div class="table-card">
  <table class="table mb-0">
    <thead><tr><th style="width:50px">Select</th><th>Item Name</th><th>Description</th><th>Unit</th><th>Rate</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
<div class="mt-3">
  <button type="submit" class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-circle"></i> Save Products</button>
</div>
</form>'''
        return self._layout(f'Products - {customer["company_name"]}', f'Products for {customer["company_name"]}', content, 'customers')


# Client-side behaviour for the Customer Management page. Kept as a plain
# string (not an f-string) so the JS braces need no escaping; __STATE__ is
# replaced with the initial filter state as JSON.
CUSTOMERS_PAGE_JS = r'''
(function () {
  var state = __STATE__;
  var debounceTimer = null;
  var selected = new Set();

  function el(id) { return document.getElementById(id); }

  function updateBulkBar() {
    var n = selected.size;
    el('cmBulkBar').classList.toggle('d-none', n === 0);
    el('cmBulkCount').textContent = n;
  }

  function syncSelectAll() {
    var boxes = document.querySelectorAll('.cm-row-check');
    el('cmSelectAll').checked = boxes.length > 0 &&
      Array.prototype.every.call(boxes, function (cb) { return cb.checked; });
  }

  function bindRowChecks() {
    document.querySelectorAll('.cm-row-check').forEach(function (cb) {
      cb.checked = selected.has(cb.value);
      cb.addEventListener('change', function () {
        if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
        updateBulkBar();
        syncSelectAll();
      });
    });
    syncSelectAll();
  }

  function updateMenus() {
    el('cmFilterLabel').textContent = state.statusLabels[state.status];
    el('cmSortLabel').textContent = state.sort === 'oldest' ? 'Sort by' : state.sortLabels[state.sort];
    document.querySelectorAll('#cmFilterMenu .cm-dd-item').forEach(function (b) {
      b.classList.toggle('active', b.dataset.value === state.status);
    });
    document.querySelectorAll('#cmSortMenu .cm-dd-item').forEach(function (b) {
      b.classList.toggle('active', b.dataset.value === state.sort);
    });
    el('cmSearchClear').classList.toggle('d-none', !state.q);
  }

  function setParam(url, key, value, isDefault) {
    if (isDefault) url.searchParams.delete(key); else url.searchParams.set(key, value);
  }

  function refresh() {
    selected.clear();
    updateBulkBar();
    var tbody = el('customersBody');
    tbody.classList.add('cm-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    params.set('page', state.page);
    params.set('sort', state.sort);
    params.set('status', state.status);
    params.set('page_size', state.pageSize);
    fetch('/api/customers/search?' + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('cm-loading');
        el('customersModals').innerHTML = data.modals_html;
        el('custPagination').innerHTML = data.pagination_html;
        el('custCountLabel').innerHTML = data.count_label;
        el('cmStatTotal').textContent = data.stats.total;
        el('cmStatActive').textContent = data.stats.active;
        el('cmStatInactive').textContent = data.stats.inactive;
        state.page = data.page;
        updateMenus();
        bindRowChecks();
        var url = new URL(window.location);
        setParam(url, 'q', state.q, !state.q);
        setParam(url, 'page', state.page, state.page <= 1);
        setParam(url, 'sort', state.sort, state.sort === 'oldest');
        setParam(url, 'status', state.status, state.status === 'all');
        setParam(url, 'page_size', state.pageSize, state.pageSize === 10);
        url.searchParams.delete('deleted');
        url.searchParams.delete('blocked');
        window.history.replaceState({}, '', url);
      })
      .catch(function () { tbody.classList.remove('cm-loading'); });
  }

  window.cmGoToPage = function (p) {
    if (p < 1) return;
    state.page = p;
    refresh();
  };
  window.cmSetStatus = function (v) { state.status = v; state.page = 1; refresh(); };
  window.cmSetSort = function (v) { state.sort = v; state.page = 1; refresh(); };
  window.cmClearSearch = function () {
    state.q = ''; state.page = 1;
    el('custSearchInput').value = '';
    refresh();
  };
  window.cmClearFilters = function () {
    state.q = ''; state.status = 'all'; state.page = 1;
    el('custSearchInput').value = '';
    refresh();
  };
  window.cmClearSelection = function () {
    selected.clear();
    document.querySelectorAll('.cm-row-check').forEach(function (cb) { cb.checked = false; });
    syncSelectAll();
    updateBulkBar();
  };

  // "Edit" button inside the View modal: close it, then open the edit modal.
  window.cmSwitchModal = function (fromId, toId) {
    var from = el(fromId), to = el(toId);
    from.addEventListener('hidden.bs.modal', function once() {
      from.removeEventListener('hidden.bs.modal', once);
      bootstrap.Modal.getOrCreateInstance(to).show();
    });
    bootstrap.Modal.getOrCreateInstance(from).hide();
  };

  var input = el('custSearchInput');
  input.addEventListener('input', function (e) {
    clearTimeout(debounceTimer);
    var val = e.target.value;
    debounceTimer = setTimeout(function () {
      state.q = val.trim(); state.page = 1; refresh();
    }, 300);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(debounceTimer);
      state.q = e.target.value.trim(); state.page = 1; refresh();
    }
  });

  el('cmSelectAll').addEventListener('change', function (e) {
    document.querySelectorAll('.cm-row-check').forEach(function (cb) {
      cb.checked = e.target.checked;
      if (e.target.checked) selected.add(cb.value); else selected.delete(cb.value);
    });
    updateBulkBar();
  });

  el('cmBulkDeleteModal').addEventListener('show.bs.modal', function () {
    el('cmBulkDeleteIds').value = Array.from(selected).join(',');
    el('cmBulkDeleteCount').textContent = selected.size;
  });

  el('cmPageSize').addEventListener('change', function (e) {
    state.pageSize = parseInt(e.target.value, 10) || 10;
    state.page = 1;
    refresh();
  });

  // Drop the one-off ?deleted=/&blocked= notice params from the address bar.
  var u = new URL(window.location);
  if (u.searchParams.has('deleted') || u.searchParams.has('blocked')) {
    u.searchParams.delete('deleted'); u.searchParams.delete('blocked');
    window.history.replaceState({}, '', u);
  }

  updateMenus();
  bindRowChecks();
})();
'''
