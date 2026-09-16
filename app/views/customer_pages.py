import json
import math
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import json_response
from database import get_db


class CustomerPagesMixin:
    CUSTOMERS_PAGE_SIZE = 10

    def handle_customer_add(self, form):
        db = get_db()
        address = form.get('address', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db.execute(
            "INSERT INTO customers "
            "(contact_person,company_name,address,phone,email,gstin) "
            "VALUES (?,?,?,?,?,?)",
            (form.get('contact_person', ''), form.get('company_name', ''),
             address, form.get('phone', ''),
             form.get('email', ''), form.get('gstin', '')))
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
        if not contact_person or not company_name:
            return self.send(*json_response(
                {'error': 'Contact person and company name are required.'},
                400))
        address = form.get('address', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db = get_db()
        cur = db.execute(
            "INSERT INTO customers "
            "(contact_person,company_name,address,phone,email,gstin) "
            "VALUES (?,?,?,?,?,?)",
            (contact_person, company_name, address, form.get('phone', ''),
             form.get('email', ''), form.get('gstin', '')))
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
        address = form.get('address', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db.execute(
            "UPDATE customers SET contact_person=?,company_name=?,address=?,"
            "phone=?,email=?,gstin=? WHERE id=?",
            (form.get('contact_person', ''), form.get('company_name', ''),
             address, form.get('phone', ''),
             form.get('email', ''), form.get('gstin', ''), cid))
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
    def _customers_rows_and_modals_html(self, rows_data):
        rows = ''
        modals = ''
        for c in rows_data:
            rows += (f'<tr><td><strong>{esc(c["contact_person"])}</strong></td>'
                     f'<td>{esc(c["company_name"])}</td>'
                     f'<td style="white-space:pre-line">{esc_nl(c["address"] or "-")}</td>'
                     f'<td>{esc(c["phone"] or "-")}</td>'
                     f'<td><small>{esc(c["gstin"] or "-")}</small></td>'
                     f'<td class="text-end">'
                     f'<a href="/customers/{c["id"]}/products" class="btn btn-sm btn-outline-success me-1" title="Manage Products"><i class="bi bi-box-seam"></i></a>'
                     f'<button class="btn btn-sm btn-outline-primary me-1" data-bs-toggle="modal" data-bs-target="#edit{c["id"]}"><i class="bi bi-pencil"></i></button>'
                     f'<form method="POST" action="/customers/{c["id"]}/delete" class="d-inline" onsubmit="return confirm(\'Delete this customer?\')">'
                     f'<button class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button></form></td></tr>')
            modals += f'''
<div class="modal fade" id="edit{c["id"]}" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/customers/{c["id"]}/edit">
      <div class="modal-header"><h5 class="modal-title">Edit Customer</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="mb-2"><label class="form-label">Contact Person</label>
          <input name="contact_person" class="form-control" value="{esc(c["contact_person"])}" required></div>
        <div class="mb-2"><label class="form-label">Company Name</label>
          <input name="company_name" class="form-control" value="{esc(c["company_name"])}" required></div>
        <div class="mb-2"><label class="form-label">Address</label>
          <textarea name="address" class="form-control" rows="4">{esc(c["address"] or "")}</textarea></div>
        <div class="row">
          <div class="col-6 mb-2"><label class="form-label">Phone</label>
            <input name="phone" class="form-control" value="{esc(c["phone"] or "")}"></div>
          <div class="col-6 mb-2"><label class="form-label">Email</label>
            <input name="email" class="form-control" value="{esc(c["email"] or "")}"></div>
        </div>
        <div class="mb-2"><label class="form-label">GSTIN</label>
          <input name="gstin" class="form-control" value="{esc(c["gstin"] or "")}"></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary"><i class="bi bi-check-lg"></i> Save</button>
      </div>
    </form>
  </div></div>
</div>'''
        return rows, modals

    def _customers_empty_state(self, has_any, q):
        """Illustration + message for zero results, whether the table is
        truly empty or just has no matches for the current search."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-people"></i></div>'
                    '<h6>No customers yet</h6>'
                    '<p class="text-muted">Once you add a customer, it will show up here.</p>'
                    '<button type="button" class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addCust">'
                    '<i class="bi bi-plus-circle"></i> Add your first customer</button>'
                    '</div>')
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching customers</h6>'
                f'<p class="text-muted">No customers match &quot;{esc(q)}&quot;.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="custClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear search</button>'
                '</div>')

    def _customers_pagination_html(self, page, total_pages):
        if total_pages <= 1:
            return ''
        html = '<nav aria-label="Customers pagination"><ul class="pagination pagination-sm q-pagination justify-content-end mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="custGoToPage(event,{p})">{label}</a></li>')

        html += page_item(page - 1, '&laquo;', disabled=(page <= 1))
        start = max(1, page - 2)
        end = min(total_pages, start + 4)
        start = max(1, end - 4)
        if start > 1:
            html += page_item(1, '1')
            if start > 2:
                html += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>'
        for p in range(start, end + 1):
            html += page_item(p, str(p), active=(p == page))
        if end < total_pages:
            if end < total_pages - 1:
                html += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>'
            html += page_item(total_pages, str(total_pages))
        html += page_item(page + 1, '&raquo;', disabled=(page >= total_pages))
        html += '</ul></nav>'
        return html

    def _customers_fragment(self, q='', page='1'):
        """Fetch, filter, and paginate customers; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/pagination without a full reload."""
        q = (q or '').strip()
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1

        db = get_db()

        where, params = [], []
        if q:
            like = f"%{q}%"
            where.append("(contact_person LIKE ? OR company_name LIKE ? OR phone LIKE ? OR gstin LIKE ?)")
            params.extend([like, like, like, like])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        has_any = db.execute("SELECT COUNT(*) c FROM customers").fetchone()['c'] > 0
        total = db.execute(f"SELECT COUNT(*) c FROM customers{where_sql}", params).fetchone()['c']
        total_pages = max(1, math.ceil(total / self.CUSTOMERS_PAGE_SIZE))
        page = min(page, total_pages)
        offset = (page - 1) * self.CUSTOMERS_PAGE_SIZE

        rows_data = db.execute(
            f"SELECT * FROM customers{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [self.CUSTOMERS_PAGE_SIZE, offset]).fetchall()
        db.close()

        if rows_data:
            rows_html, modals_html = self._customers_rows_and_modals_html(rows_data)
        else:
            rows_html = f'<tr><td colspan="6" class="p-0">{self._customers_empty_state(has_any, q)}</td></tr>'
            modals_html = ''

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + self.CUSTOMERS_PAGE_SIZE, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total}'
                       if total else 'No customers found')

        return {
            'rows_html': rows_html,
            'modals_html': modals_html,
            'pagination_html': self._customers_pagination_html(page, total_pages),
            'count_label': count_label,
            'total': total,
            'page': page,
            'total_pages': total_pages,
        }

    def pg_customers(self, q='', page='1'):
        frag = self._customers_fragment(q=q, page=page)
        content = f'''
<style>
.q-empty {{ text-align:center; padding:56px 20px; }}
.q-empty-icon {{ width:64px; height:64px; border-radius:50%; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:26px; margin:0 auto 16px; }}
.q-empty h6 {{ margin-bottom:6px; font-weight:600; color:#0F172A; }}
.q-count-label {{ font-size:13px; color:#64748B; }}
#customersBody {{ transition:opacity .1s; }}
#customersBody.q-loading {{ opacity:.4; }}
.q-pagination .page-link {{ color:var(--primary); }}
.q-pagination .page-item.active .page-link {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-pagination .page-item.disabled .page-link {{ color:#94A3B8; }}
</style>

<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
  <h5 class="mb-0"><i class="bi bi-people"></i> Customers (<span id="custHeaderCount">{frag["total"]}</span>)</h5>
  <div class="d-flex align-items-center gap-2 flex-wrap">
    <div class="input-group input-group-sm" style="max-width:280px">
      <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
      <input type="text" id="custSearchInput" class="form-control border-start-0" placeholder="Search customers..." value="{esc(q)}">
    </div>
    <button class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addCust">
      <i class="bi bi-plus-circle"></i> Add Customer</button>
  </div>
</div>
<div class="table-card">
  <table class="table mb-0">
    <thead><tr><th>Contact</th><th>Company</th><th>Address</th><th>Phone</th><th>GSTIN</th><th class="text-end">Actions</th></tr></thead>
    <tbody id="customersBody">{frag["rows_html"]}</tbody>
  </table>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="custCountLabel">{frag["count_label"]}</span>
    <div id="custPagination">{frag["pagination_html"]}</div>
  </div>
</div>
<div id="customersModals">{frag["modals_html"]}</div>
<div class="modal fade" id="addCust" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/customers/add">
      <div class="modal-header"><h5 class="modal-title">Add Customer</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="mb-2"><label class="form-label">Contact Person</label>
          <input name="contact_person" class="form-control" required></div>
        <div class="mb-2"><label class="form-label">Company Name</label>
          <input name="company_name" class="form-control" required></div>
        <div class="mb-2"><label class="form-label">Address</label>
          <textarea name="address" class="form-control" rows="4" placeholder="e.g. STREET NO.1, ANAND NAGAR&#10;NEAR KIDZ NEST SCHOOL, MAKSUDAN&#10;JALANDHAR (PUNJAB), INDIA&#10;PIN CODE - 144008"></textarea></div>
        <div class="row">
          <div class="col-6 mb-2"><label class="form-label">Phone</label>
            <input name="phone" class="form-control"></div>
          <div class="col-6 mb-2"><label class="form-label">Email</label>
            <input name="email" class="form-control"></div>
        </div>
        <div class="mb-2"><label class="form-label">GSTIN</label>
          <input name="gstin" class="form-control"></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary"><i class="bi bi-plus-lg"></i> Add</button>
      </div>
    </form>
  </div></div>
</div>
<script>
(function() {{
  var state = {{ q: {json.dumps(q)}, page: {frag["page"]} }};
  var debounceTimer = null;

  function refresh() {{
    var tbody = document.getElementById('customersBody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    params.set('page', state.page);
    fetch('/api/customers?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('customersModals').innerHTML = data.modals_html;
        document.getElementById('custPagination').innerHTML = data.pagination_html;
        document.getElementById('custCountLabel').innerHTML = data.count_label;
        document.getElementById('custHeaderCount').textContent = data.total;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.custGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.custClearFilters = function() {{
    state.q = '';
    state.page = 1;
    document.getElementById('custSearchInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('custSearchInput');
  searchInput.addEventListener('input', function(e) {{
    clearTimeout(debounceTimer);
    var val = e.target.value;
    debounceTimer = setTimeout(function() {{
      state.q = val;
      state.page = 1;
      refresh();
    }}, 350);
  }});
  searchInput.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{
      e.preventDefault();
      clearTimeout(debounceTimer);
      state.q = e.target.value;
      state.page = 1;
      refresh();
    }}
  }});
}})();
</script>'''
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
  <h5 class="mb-0"><i class="bi bi-box-seam"></i> Products for {esc(customer["company_name"])}</h5>
  <a href="/customers" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
</div>
<form method="POST" action="/customers/{cid}/items">
<div class="table-card">
  <table class="table mb-0">
    <thead><tr><th style="width:50px">Select</th><th>Item Name</th><th>Description</th><th>Unit</th><th>Rate</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
<div class="mt-3">
  <button type="submit" class="btn btn-primary"><i class="bi bi-check-circle"></i> Save Products</button>
</div>
</form>'''
        return self._layout(f'Products - {customer["company_name"]}', f'Products for {customer["company_name"]}', content, 'customers')


