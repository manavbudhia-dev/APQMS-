import json
import math
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import pdf_response
from database import get_db
from database import gen_proforma_no
from app.generators.proforma_generator import build_proforma_pdf
from datetime import datetime


class ProformaPagesMixin:
    PROFORMA_STATUSES = ['Draft', 'Sent', 'Confirmed', 'Cancelled']
    PROFORMA_PAGE_SIZE = 10

    def handle_proforma_create(self, form, user_id):
        db = get_db()
        pino = gen_proforma_no(db)
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        order_date = form.get('order_date', '')
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None
        cur = db.execute(
            "INSERT INTO proforma_invoices "
            "(pi_no,order_no,order_date,date,customer_id,subject,status,notes,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pino, form.get('order_no', ''), order_date, date, customer_id, form.get('subject', ''),
             'Draft', form.get('notes', ''), user_id))
        pid = cur.lastrowid

        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        for i, (iid, qty) in enumerate(zip(item_ids, quantities)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO proforma_invoice_items "
                    "(proforma_id,item_id,item_name,description,specification,"
                    "quantity,unit,rate,gst_percent,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'],
                     item['rate_per_unit'], 18, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/proforma/{pid}'))


    def handle_proforma_edit(self, pid, form, user_id):
        db = get_db()
        # Check if proforma exists
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.send(*redirect('/proforma'))

        # Update proforma invoice
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        order_date = form.get('order_date', '')
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        db.execute(
            "UPDATE proforma_invoices SET order_no=?, order_date=?, date=?, customer_id=?, "
            "subject=?, notes=? WHERE id=?",
            (form.get('order_no', ''), order_date, date, customer_id,
             form.get('subject', ''), form.get('notes', ''), pid))

        # Delete existing items
        db.execute("DELETE FROM proforma_invoice_items WHERE proforma_id=?", (pid,))

        # Add new items
        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        for i, (iid, qty) in enumerate(zip(item_ids, quantities)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO proforma_invoice_items "
                    "(proforma_id,item_id,item_name,description,specification,"
                    "quantity,unit,rate,gst_percent,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'],
                     item['rate_per_unit'], 18, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/proforma/{pid}'))


    def _gen_pi_pdf(self, pid):
        db = get_db()
        row = db.execute("SELECT * FROM proforma_invoices WHERE id=?",
                         (pid,)).fetchone()
        if not row:
            db.close()
            return pdf_response(b'%PDF-1.4\n% Not found', 'not-found.pdf')
        p = dict(row)
        c = None
        if p.get('customer_id'):
            crow = db.execute("SELECT * FROM customers WHERE id=?",
                              (p['customer_id'],)).fetchone()
            c = dict(crow) if crow else None
        items = [dict(r) for r in db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? "
            "ORDER BY sort_order", (pid,)).fetchall()]
        db.close()
        pdf = build_proforma_pdf(p, c, items)
        return pdf_response(pdf, f"{p['pi_no']}.pdf")


    # ── PAGE: PROFORMA INVOICES LIST ─────────────────────────
    def _proforma_rows_html(self, rows_data):
        """Render <tr> rows for a set of proforma invoice records (already fetched)."""
        rows = ''
        for p in rows_data:
            rows += (f'<tr><td><strong>{esc(p["pi_no"])}</strong></td>'
                     f'<td>{esc(p["order_no"] or "-")}</td>'
                     f'<td>{esc(p["date"])}</td>'
                     f'<td>{esc(p["contact_person"] or "-")}</td>'
                     f'<td>{esc(p["company_name"] or "-")}</td>'
                     f'<td>{esc(p["subject"] or "-")}</td>'
                     f'<td><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td>'
                     f'<td class="text-end">'
                     f'<a href="/proforma/{p["id"]}" class="btn btn-sm btn-outline-primary me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/proforma/{p["id"]}/pdf" class="btn btn-sm btn-outline-danger" title="Download PDF"><i class="bi bi-file-earmark-pdf"></i></a>'
                     f'</td></tr>')
        return rows

    def _proforma_empty_state(self, has_any, q, status):
        """Illustration + message for zero results, whether the table is
        truly empty or just has no matches for the current search/filter."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-receipt"></i></div>'
                    '<h6>No proforma invoices yet</h6>'
                    '<p class="text-muted">Once you create a proforma invoice, it will show up here.</p>'
                    '<a href="/proforma/new" class="btn btn-primary btn-sm">'
                    '<i class="bi bi-plus-circle"></i> Create your first proforma invoice</a>'
                    '</div>')
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching proforma invoices</h6>'
                '<p class="text-muted">Try a different search term'
                + (f' or clear the "{esc(status)}" filter' if status else '')
                + '.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="piClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _proforma_pagination_html(self, page, total_pages, q, status):
        if total_pages <= 1:
            return ''
        html = '<nav aria-label="Proforma pagination"><ul class="pagination pagination-sm q-pagination justify-content-end mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="piGoToPage(event,{p})">{label}</a></li>')

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

    def _proforma_chips_html(self, status_counts, all_count, status):
        chips = [('', 'All', all_count)] + [(s, s, status_counts.get(s, 0)) for s in self.PROFORMA_STATUSES]
        html = ''
        for val, label, count in chips:
            active = ' active' if val == status else ''
            html += (f'<button type="button" class="q-chip{active}" data-status="{esc(val)}" '
                     f'onclick="piSetStatus(\'{esc(val)}\')">{esc(label)} '
                     f'<span class="q-chip-count">{count}</span></button>')
        return html

    def _proforma_fragment(self, q='', status='', page='1'):
        """Fetch, filter, and paginate proforma invoices; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/filter/pagination without a full reload."""
        q = (q or '').strip()
        status = status or ''
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        if status not in self.PROFORMA_STATUSES:
            status = ''

        db = get_db()

        search_where, search_params = [], []
        if q:
            like = f"%{q}%"
            search_where.append(
                "(p.pi_no LIKE ? OR p.order_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR p.date LIKE ?)")
            search_params.extend([like, like, like, like, like])
        search_sql = (" WHERE " + " AND ".join(search_where)) if search_where else ""

        has_any = db.execute("SELECT COUNT(*) c FROM proforma_invoices").fetchone()['c'] > 0

        all_count = db.execute(
            f"SELECT COUNT(*) c FROM proforma_invoices p LEFT JOIN customers c ON p.customer_id=c.id{search_sql}",
            search_params).fetchone()['c']
        status_counts = {}
        for s in self.PROFORMA_STATUSES:
            join = " AND" if search_where else " WHERE"
            r = db.execute(
                f"SELECT COUNT(*) c FROM proforma_invoices p LEFT JOIN customers c ON p.customer_id=c.id{search_sql}{join} p.status=?",
                search_params + [s]).fetchone()
            status_counts[s] = r['c']

        where = list(search_where)
        params = list(search_params)
        if status:
            where.append("p.status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = db.execute(
            f"SELECT COUNT(*) c FROM proforma_invoices p LEFT JOIN customers c ON p.customer_id=c.id{where_sql}",
            params).fetchone()['c']
        total_pages = max(1, math.ceil(total / self.PROFORMA_PAGE_SIZE))
        page = min(page, total_pages)
        offset = (page - 1) * self.PROFORMA_PAGE_SIZE

        rows_data = db.execute(
            f"SELECT p.*,c.company_name,c.contact_person FROM proforma_invoices p "
            f"LEFT JOIN customers c ON p.customer_id=c.id{where_sql} "
            f"ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            params + [self.PROFORMA_PAGE_SIZE, offset]).fetchall()
        db.close()

        if rows_data:
            rows_html = self._proforma_rows_html(rows_data)
        else:
            rows_html = f'<tr><td colspan="8" class="p-0">{self._proforma_empty_state(has_any, q, status)}</td></tr>'

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + self.PROFORMA_PAGE_SIZE, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total}'
                       if total else 'No proforma invoices found')

        return {
            'rows_html': rows_html,
            'pagination_html': self._proforma_pagination_html(page, total_pages, q, status),
            'chips_html': self._proforma_chips_html(status_counts, all_count, status),
            'count_label': count_label,
            'total': total,
            'page': page,
            'total_pages': total_pages,
        }

    def pg_pilist(self, q='', status='', page='1'):
        frag = self._proforma_fragment(q=q, status=status, page=page)
        content = f'''
<style>
.q-toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; margin-bottom:14px; }}
.q-chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
.q-chip {{ border:1px solid #E2E8F0; background:#fff; color:#374151; border-radius:999px; padding:5px 12px; font-size:13px; font-weight:500; display:inline-flex; align-items:center; gap:6px; cursor:pointer; transition:all .15s; }}
.q-chip:hover {{ border-color:var(--primary); color:var(--primary); }}
.q-chip.active {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-chip-count {{ background:rgba(0,0,0,.08); border-radius:999px; padding:0 7px; font-size:11.5px; }}
.q-chip.active .q-chip-count {{ background:rgba(255,255,255,.25); }}
.q-empty {{ text-align:center; padding:56px 20px; }}
.q-empty-icon {{ width:64px; height:64px; border-radius:50%; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:26px; margin:0 auto 16px; }}
.q-empty h6 {{ margin-bottom:6px; font-weight:600; color:#0F172A; }}
.q-count-label {{ font-size:13px; color:#64748B; }}
#proformaTbody {{ transition:opacity .1s; }}
#proformaTbody.q-loading {{ opacity:.4; }}
.q-pagination .page-link {{ color:var(--primary); }}
.q-pagination .page-item.active .page-link {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-pagination .page-item.disabled .page-link {{ color:#94A3B8; }}
</style>

<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-receipt"></i> Proforma Invoices (<span id="piHeaderCount">{frag["total"]}</span>)</h5>
  <a href="/proforma/new" class="btn btn-primary btn-sm"><i class="bi bi-plus-circle"></i> New Proforma Invoice</a>
</div>

<div class="table-card">
  <div class="p-3 border-bottom">
    <div class="q-toolbar">
      <div class="input-group input-group-sm" style="max-width:320px">
        <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
        <input type="text" id="piSearchInput" class="form-control border-start-0" placeholder="Search by no., order no., customer, or date" value="{esc(q)}">
      </div>
      <div class="q-chips" id="piChips">{frag["chips_html"]}</div>
    </div>
  </div>
  <table class="table mb-0">
    <thead><tr><th>No</th><th>Order No</th><th>Date</th><th>Contact</th><th>Company</th><th>Subject</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
    <tbody id="proformaTbody">{frag["rows_html"]}</tbody>
  </table>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="piCountLabel">{frag["count_label"]}</span>
    <div id="piPagination">{frag["pagination_html"]}</div>
  </div>
</div>

<script>
(function() {{
  var state = {{ q: {json.dumps(q)}, status: {json.dumps(status)}, page: {frag["page"]} }};
  var debounceTimer = null;

  function refresh() {{
    var tbody = document.getElementById('proformaTbody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    params.set('page', state.page);
    fetch('/api/proforma?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('piPagination').innerHTML = data.pagination_html;
        document.getElementById('piChips').innerHTML = data.chips_html;
        document.getElementById('piCountLabel').innerHTML = data.count_label;
        document.getElementById('piHeaderCount').textContent = data.total;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.status) url.searchParams.set('status', state.status); else url.searchParams.delete('status');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.piGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.piSetStatus = function(s) {{
    state.status = s;
    state.page = 1;
    refresh();
  }};

  window.piClearFilters = function() {{
    state.q = '';
    state.status = '';
    state.page = 1;
    document.getElementById('piSearchInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('piSearchInput');
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
        return self._layout('Proforma Invoices', 'All Proforma Invoices', content, 'proforma')


    # ── PAGE: NEW PROFORMA INVOICE ────────────────────────────
    def pg_pinew(self):
        db = get_db()
        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        pino = gen_proforma_no(db)
        today = datetime.now().strftime('%d - %m - %Y')
        db.close()

        cust_opts = '<option value="">- Select Customer -</option>'
        for c in customers:
            cust_opts += (f'<option value="{c["id"]}" '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        item_opts = '<option value="">- Pick an item -</option>'
        for i in items:
            item_opts += (f'<option value="{i["id"]}" '
                          f'data-name="{esc(i["item_name"])}" '
                          f'data-desc="{esc(i["description"] or "")}" '
                          f'data-spec="{esc(i["specification"] or "")}" '
                          f'data-unit="{esc(i["unit"])}" '
                          f'data-rate="{i["rate_per_unit"]}">'
                          f'{esc(i["item_name"])} - Rs. {fmt_rupee(i["rate_per_unit"])}/{esc(i["unit"])}</option>')

        js = """
let piItems = [];
let piCounter = 0;
let subjectManual = false;

function fillCustPI() {
  const sel = document.getElementById('custSelPI');
  const info = document.getElementById('custInfoPI');
  if (sel.value) {
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePI').textContent = opt.dataset.name;
    document.getElementById('cCompanyPI').textContent = opt.dataset.company;
    document.getElementById('cAddrPI').textContent = opt.dataset.address;
    info.style.display = 'block';
    loadItemsForCustomerPI(sel.value);
  } else {
    info.style.display = 'none';
    loadAllItemsPI();
  }
}

function loadItemsForCustomerPI(cid) {
  fetch('/api/customers/' + cid + '/items')
    .then(r => r.json())
    .then(items => {
      const sel = document.getElementById('itemSelPI');
      sel.innerHTML = '<option value="">- Pick an item -</option>';
      items.forEach(i => {
        sel.innerHTML += '<option value="' + i.id + '" data-name="' + i.item_name.replace(/"/g, '&quot;') + '" data-desc="' + (i.description||'').replace(/"/g, '&quot;') + '" data-spec="' + (i.specification||'').replace(/"/g, '&quot;') + '" data-unit="' + i.unit + '" data-rate="' + i.rate_per_unit + '">' + i.item_name + ' - Rs. ' + Number(i.rate_per_unit).toLocaleString('en-IN') + '/' + i.unit + '</option>';
      });
    });
}

function loadAllItemsPI() {
  fetch('/api/items')
    .then(r => r.json())
    .then(items => {
      const sel = document.getElementById('itemSelPI');
      sel.innerHTML = '<option value="">- Pick an item -</option>';
      items.forEach(i => {
        sel.innerHTML += '<option value="' + i.id + '" data-name="' + i.item_name.replace(/"/g, '&quot;') + '" data-desc="' + (i.description||'').replace(/"/g, '&quot;') + '" data-spec="' + (i.specification||'').replace(/"/g, '&quot;') + '" data-unit="' + i.unit + '" data-rate="' + i.rate_per_unit + '">' + i.item_name + ' - Rs. ' + Number(i.rate_per_unit).toLocaleString('en-IN') + '/' + i.unit + '</option>';
      });
    });
}

function showSpecPI() {
  const sel = document.getElementById('itemSelPI');
  const box = document.getElementById('specBoxPI');
  if (sel.value) {
    const opt = sel.options[sel.selectedIndex];
    const rate = Number(opt.dataset.rate).toLocaleString('en-IN', {maximumFractionDigits: 0});
    document.getElementById('specTextPI').innerHTML =
      '<strong>' + opt.dataset.name + '</strong><br>' +
      '<em>' + opt.dataset.desc + '</em><br><br>' +
      '<b>Specification:</b><br>' + opt.dataset.spec.replace(/\\n/g, '<br>') +
      '<br><br><b>Rate:</b> Rs. ' + rate + ' / ' + opt.dataset.unit;
    box.style.display = 'block';
  } else {
    box.style.display = 'none';
  }
}

function addItemPI() {
  const sel = document.getElementById('itemSelPI');
  const qty = parseFloat(document.getElementById('itemQtyPI').value);
  if (!sel.value) { alert('Please select an item.'); return; }
  if (!qty || qty <= 0) { alert('Please enter a valid quantity.'); return; }
  const opt = sel.options[sel.selectedIndex];
  piCounter++;
  piItems.push({
    id: piCounter, itemId: sel.value, name: opt.dataset.name,
    desc: opt.dataset.desc, spec: opt.dataset.spec,
    unit: opt.dataset.unit, rate: parseFloat(opt.dataset.rate), qty: qty
  });
  renderItemsPI();
  sel.value = '';
  document.getElementById('itemQtyPI').value = '';
  document.getElementById('specBoxPI').style.display = 'none';
}

function removeItemPI(id) {
  piItems = piItems.filter(function(i) { return i.id !== id; });
  renderItemsPI();
}

function renderItemsPI() {
  const list = document.getElementById('itemsListPI');
  if (piItems.length === 0) {
    list.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><p>No items added yet.</p></div>';
    updSumPI();
    return;
  }
  let html = '';
  piItems.forEach(function(item, idx) {
    const amt = item.qty * item.rate;
    html += '<div class="quotation-item-row"><div class="d-flex justify-content-between align-items-start"><div>' +
      '<span class="item-name">' + (idx+1) + '. ' + item.name + '</span>' +
      '<input type="hidden" name="item_id[]" value="' + item.itemId + '">' +
      '<input type="hidden" name="quantity[]" value="' + item.qty + '">' +
      '<div class="item-spec">' + item.spec.replace(/\\n/g, '<br>') + '</div>' +
      '<small class="text-muted">Rate: Rs. ' + item.rate.toLocaleString('en-IN', {maximumFractionDigits: 0}) + ' / ' + item.unit + '</small>' +
      '</div><div class="text-end"><div class="item-rate">Rs. ' + amt.toLocaleString('en-IN', {maximumFractionDigits: 0}) + '</div>' +
      '<button type="button" class="btn btn-sm btn-outline-danger mt-1" onclick="removeItemPI(' + item.id + ')"><i class="bi bi-x-circle"></i> Remove</button>' +
      '</div></div></div>';
  });
  list.innerHTML = html;
  updSumPI();
}

function updSumPI() {
  let subtotal = 0;
  piItems.forEach(function(item) { subtotal += item.qty * item.rate; });
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  const loc = {maximumFractionDigits: 0};
  document.getElementById('sCnt').textContent = piItems.length;
  document.getElementById('sSub').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN', loc);
  document.getElementById('sGst').textContent = 'Rs. ' + gst.toLocaleString('en-IN', loc);
  document.getElementById('sGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN', loc);
  const subj = document.getElementById('subjInput');
  if (!subjectManual) {
    if (piItems.length === 1) {
      subj.value = piItems[0].name;
    } else if (piItems.length > 1) {
      subj.value = 'Chains';
    } else {
      subj.value = '';
    }
  }
}
"""

        content = f'''
<form method="POST" action="/proforma/new" id="piForm">
<div class="row g-3">
  <div class="col-lg-7">

    <div class="form-section mb-3">
      <h5><i class="bi bi-info-circle"></i> Proforma Invoice Info</h5>
      <div class="row g-2">
        <div class="col-md-3"><label class="form-label">PI No</label>
          <input class="form-control" value="{esc(pino)}" readonly style="background:#F8FAFC"></div>
        <div class="col-md-3"><label class="form-label">Order No</label>
          <input name="order_no" class="form-control" placeholder="Customer order ref"></div>
        <div class="col-md-3"><label class="form-label">Order Date</label>
          <input type="date" name="order_date" class="form-control"></div>
        <div class="col-md-3"><label class="form-label">Date</label>
          <input name="date" class="form-control" value="{esc(today)}"></div>
      </div>
      <div class="row g-2 mt-1">
        <div class="col-md-3"><label class="form-label">Status</label>
          <input class="form-control" value="Draft" readonly style="background:#F8FAFC"></div>
      </div>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-person"></i> Customer</h5>
      <select name="customer_id" class="form-select" id="custSelPI" onchange="fillCustPI()">{cust_opts}</select>
      <div class="mt-2 p-3 bg-light rounded" id="custInfoPI" style="display:none">
        <div class="fw-bold" id="cNamePI"></div>
        <div id="cCompanyPI"></div>
        <small class="text-muted" id="cAddrPI" style="white-space:pre-line"></small>
      </div>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-cart-plus"></i> Add Items</h5>
      <div class="row g-2 mb-2">
        <div class="col-md-5"><label class="form-label">Select Item</label>
          <select class="form-select" id="itemSelPI" onchange="showSpecPI()">{item_opts}</select></div>
        <div class="col-md-2"><label class="form-label">Qty</label>
          <input type="number" class="form-control" id="itemQtyPI" min="0" step="0.01" placeholder="0"></div>
        <div class="col-md-2"><label class="form-label">&nbsp;</label>
          <button type="button" class="btn btn-primary w-100" onclick="addItemPI()"><i class="bi bi-plus"></i> Add</button></div>
      </div>
      <div class="p-3 bg-light rounded" id="specBoxPI" style="display:none">
        <div id="specTextPI" style="font-size:12px;white-space:pre-line;color:#475569"></div>
      </div>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-list-ol"></i> Items in Proforma Invoice</h5>
      <div id="itemsListPI">
        <div class="empty-state"><i class="bi bi-inbox"></i><p>No items added yet.</p></div>
      </div>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-text-left"></i> Subject</h5>
      <div class="mb-2"><label class="form-label">Subject</label>
        <input name="subject" class="form-control" id="subjInput" placeholder="e.g. Sugar Elevator Chain" oninput="subjectManual = this.value.trim() !== ''"></div>
      <div class="mb-2"><label class="form-label">Notes (internal)</label>
        <textarea name="notes" class="form-control" rows="2" placeholder="Optional internal notes"></textarea></div>
    </div>

  </div>

  <div class="col-lg-5">
    <div class="form-section mb-3" style="position:sticky;top:70px">
      <h5><i class="bi bi-calculator"></i> Summary</h5>
      <div class="summary-box">
        <div class="total-line"><span>Items:</span><strong id="sCnt">0</strong></div>
        <div class="total-line"><span>Sub-Total:</span><strong id="sSub">Rs. 0</strong></div>
        <div class="total-line"><span>GST (18%):</span><strong id="sGst">Rs. 0</strong></div>
        <div class="total-line grand-total"><span>Grand Total:</span><strong id="sGrand">Rs. 0</strong></div>
      </div>
      <div class="mt-3">
        <button type="submit" class="btn btn-primary w-100 btn-lg"><i class="bi bi-check-circle"></i> Create Proforma Invoice</button>
      </div>
      <div class="mt-2 text-center">
        <small class="text-muted">PDF will be available after creation</small>
      </div>
    </div>
  </div>
</div>
</form>
<script>{js}</script>'''
        return self._layout('New Proforma Invoice', 'Create Proforma Invoice', content, 'proforma')


    # ── PAGE: EDIT PROFORMA INVOICE ─────────────────────────
    def pg_piedit(self, pid):
        db = get_db()
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()

        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        pi_items = db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? ORDER BY sort_order",
            (pid,)).fetchall()
        db.close()

        cust_opts = '<option value="">- Select Customer -</option>'
        for c in customers:
            sel = ' selected' if p['customer_id'] == c['id'] else ''
            cust_opts += (f'<option value="{c["id"]}"{sel} '
                          f'data-name="{esc(c["contact_person"])}" '
                          f'data-company="{esc(c["company_name"])}" '
                          f'data-address="{esc(c["address"] or "")}">'
                          f'{esc(c["company_name"])} ({esc(c["contact_person"])})</option>')

        item_opts = '<option value="">- Pick an item -</option>'
        for i in items:
            item_opts += (f'<option value="{i["id"]}" '
                          f'data-name="{esc(i["item_name"])}" '
                          f'data-desc="{esc(i["description"] or "")}" '
                          f'data-spec="{esc(i["specification"] or "")}" '
                          f'data-unit="{esc(i["unit"])}" '
                          f'data-rate="{i["rate_per_unit"]}">'
                          f'{esc(i["item_name"])} - Rs. {fmt_rupee(i["rate_per_unit"])}/{esc(i["unit"])}</option>')

        # Pre-populate items
        def _js_str(val):
            """Safely embed arbitrary text inside a JS double-quoted string
            literal. Order matters: backslashes must be escaped first, or
            the escaping of quotes/newlines would double up. Without this,
            fields containing raw newlines (e.g. multi-line descriptions)
            produce invalid JS ('unterminated string literal'), which
            silently breaks the entire inline <script> block and prevents
            existing items from ever being rendered on edit."""
            s = esc(val or '')
            s = s.replace(chr(92), chr(92) + chr(92))   # backslash -> \\
            s = s.replace(chr(34), chr(92) + chr(34))   # "        -> \"
            s = s.replace(chr(10), chr(92) + 'n')        # \n       -> \n
            s = s.replace(chr(13), chr(92) + 'r')        # \r       -> \r
            return s

        existing_items_js = '['
        for idx, it in enumerate(pi_items):
            if idx > 0:
                existing_items_js += ','
            existing_items_js += '{'
            existing_items_js += f'id: {idx + 1}, '
            existing_items_js += f'itemId: {it["item_id"]}, '
            existing_items_js += f'name: "{_js_str(it["item_name"])}", '
            existing_items_js += f'desc: "{_js_str(it["description"])}", '
            existing_items_js += f'spec: "{_js_str(it["specification"])}", '
            existing_items_js += f'unit: "{_js_str(it["unit"])}", '
            existing_items_js += f'rate: {it["rate"]}, '
            existing_items_js += f'qty: {it["quantity"]}'
            existing_items_js += '}'
        existing_items_js += ']'

        js = f"""
let piItems = {existing_items_js};
let piCounter = {len(pi_items)};

function fillCustPI() {{
  const sel = document.getElementById('custSelPI');
  const info = document.getElementById('custInfoPI');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePI').textContent = opt.dataset.name;
    document.getElementById('cCompanyPI').textContent = opt.dataset.company;
    document.getElementById('cAddrPI').textContent = opt.dataset.address;
    info.style.display = 'block';
    loadItemsForCustomerPI(sel.value);
  }} else {{
    info.style.display = 'none';
    loadAllItemsPI();
  }}
}}

function loadItemsForCustomerPI(cid) {{
  fetch('/api/customers/' + cid + '/items')
    .then(r => r.json())
    .then(items => {{
      const sel = document.getElementById('itemSelPI');
      sel.innerHTML = '<option value="">- Pick an item -</option>';
      items.forEach(i => {{
        sel.innerHTML += '<option value="' + i.id + '" data-name="' + i.item_name.replace(/"/g, '&quot;') + '" data-desc="' + (i.description||'').replace(/"/g, '&quot;') + '" data-spec="' + (i.specification||'').replace(/"/g, '&quot;') + '" data-unit="' + i.unit + '" data-rate="' + i.rate_per_unit + '">' + i.item_name + ' - Rs. ' + Number(i.rate_per_unit).toLocaleString('en-IN') + '/' + i.unit + '</option>';
      }});
    }});
}}

function loadAllItemsPI() {{
  fetch('/api/items')
    .then(r => r.json())
    .then(items => {{
      const sel = document.getElementById('itemSelPI');
      sel.innerHTML = '<option value="">- Pick an item -</option>';
      items.forEach(i => {{
        sel.innerHTML += '<option value="' + i.id + '" data-name="' + i.item_name.replace(/"/g, '&quot;') + '" data-desc="' + (i.description||'').replace(/"/g, '&quot;') + '" data-spec="' + (i.specification||'').replace(/"/g, '&quot;') + '" data-unit="' + i.unit + '" data-rate="' + i.rate_per_unit + '">' + i.item_name + ' - Rs. ' + Number(i.rate_per_unit).toLocaleString('en-IN') + '/' + i.unit + '</option>';
      }});
    }});
}}

function showSpecPI() {{
  const sel = document.getElementById('itemSelPI');
  const box = document.getElementById('specBoxPI');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    const rate = Number(opt.dataset.rate).toLocaleString('en-IN', {{maximumFractionDigits: 0}});
    document.getElementById('specTextPI').innerHTML =
      '<strong>' + opt.dataset.name + '</strong><br>' +
      '<em>' + opt.dataset.desc + '</em><br><br>' +
      '<b>Specification:</b><br>' + opt.dataset.spec.replace(/\\n/g, '<br>') +
      '<br><br><b>Rate:</b> Rs. ' + rate + ' / ' + opt.dataset.unit;
    box.style.display = 'block';
  }} else {{
    box.style.display = 'none';
  }}
}}

function addItemPI() {{
  const sel = document.getElementById('itemSelPI');
  const qty = parseFloat(document.getElementById('itemQtyPI').value);
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  if (!qty || qty <= 0) {{ alert('Please enter a valid quantity.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  piCounter++;
  piItems.push({{
    id: piCounter, itemId: sel.value, name: opt.dataset.name,
    desc: opt.dataset.desc, spec: opt.dataset.spec,
    unit: opt.dataset.unit, rate: parseFloat(opt.dataset.rate), qty: qty
  }});
  renderItemsPI();
  sel.value = '';
  document.getElementById('itemQtyPI').value = '1';
  document.getElementById('specBoxPI').style.display = 'none';
}}

function removeItemPI(id) {{
  piItems = piItems.filter(it => it.id !== id);
  renderItemsPI();
}}

function renderItemsPI() {{
  let tbody = document.getElementById('piItemsTbody');
  if (piItems.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No items added yet.</td></tr>';
    document.getElementById('piTotalBox').style.display = 'none';
    return;
  }}
  let html = '';
  let subtotal = 0;
  piItems.forEach((it, idx) => {{
    const amt = it.qty * it.rate;
    subtotal += amt;
    html += '<tr><td class="text-center">' + (idx + 1) + '</td>';
    html += '<td><strong>' + it.name + '</strong><br><small class="text-muted">' + it.desc + '</small></td>';
    html += '<td style="font-size:11px">' + it.spec.replace(/\\n/g, '<br>') + '</td>';
    html += '<td class="text-center">' + it.qty + '</td>';
    html += '<td class="text-center">' + it.unit + '</td>';
    html += '<td class="text-end">' + it.rate.toLocaleString('en-IN') + '</td>';
    html += '<td class="text-end"><strong>' + amt.toLocaleString('en-IN') + '</strong></td>';
    html += '<td class="text-center"><button type="button" class="btn btn-sm btn-outline-danger" onclick="removeItemPI(' + it.id + ')"><i class="bi bi-trash"></i></button></td></tr>';
  }});
  tbody.innerHTML = html;
  const gst = subtotal * 0.18;
  const grand = subtotal + gst;
  document.getElementById('piSubtotal').textContent = 'Rs. ' + subtotal.toLocaleString('en-IN');
  document.getElementById('piGST').textContent = 'Rs. ' + gst.toLocaleString('en-IN');
  document.getElementById('piGrand').textContent = 'Rs. ' + grand.toLocaleString('en-IN');
  document.getElementById('piTotalBox').style.display = 'block';
}}

function submitPI() {{
  if (piItems.length === 0) {{ alert('Please add at least one item.'); return false; }}
  const form = document.getElementById('piForm');
  piItems.forEach(it => {{
    const inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'item_id[]';
    inp.value = it.itemId;
    form.appendChild(inp);
    const qtyInp = document.createElement('input');
    qtyInp.type = 'hidden';
    qtyInp.name = 'quantity[]';
    qtyInp.value = it.qty;
    form.appendChild(qtyInp);
  }});
  return true;
}}

// Initialize on load
window.addEventListener('DOMContentLoaded', function() {{
  renderItemsPI();
  fillCustPI();
}});
"""

        content = f'''
<form id="piForm" method="POST" action="/proforma/{pid}/edit" onsubmit="return submitPI()">
  <div class="row g-3">
    <div class="col-lg-8">
      <div class="form-section">
        <h5><i class="bi bi-receipt"></i> Edit Proforma Invoice</h5>
        <div class="row g-3">
          <div class="col-md-3">
            <label class="form-label">PI No.</label>
            <input type="text" class="form-control" value="{esc(p['pi_no'])}" readonly>
          </div>
          <div class="col-md-3">
            <label class="form-label">Order No.</label>
            <input name="order_no" class="form-control" value="{esc(p['order_no'] or '')}">
          </div>
          <div class="col-md-3">
            <label class="form-label">Order Date</label>
            <input type="date" name="order_date" class="form-control" value="{esc(p['order_date'] or '')}">
          </div>
          <div class="col-md-3">
            <label class="form-label">Date</label>
            <input name="date" class="form-control" value="{esc(p['date'])}">
          </div>
        </div>
        <div class="mt-3">
          <label class="form-label">Customer</label>
          <select id="custSelPI" name="customer_id" class="form-select" onchange="fillCustPI()">
            {cust_opts}
          </select>
        </div>
        <div id="custInfoPI" class="alert alert-info mt-3" style="display:{'block' if p['customer_id'] else 'none'}">
          <strong id="cNamePI"></strong><br>
          <span id="cCompanyPI"></span><br>
          <span id="cAddrPI" style="white-space:pre-line"></span>
        </div>
      </div>

      <div class="form-section">
        <h5><i class="bi bi-list-ul"></i> Items</h5>
        <div class="row g-2 mb-3">
          <div class="col-md-6">
            <select id="itemSelPI" class="form-select" onchange="showSpecPI()">
              {item_opts}
            </select>
          </div>
          <div class="col-md-2">
            <input id="itemQtyPI" type="number" step="0.01" min="0.01" class="form-control" placeholder="Qty" value="1">
          </div>
          <div class="col-md-2">
            <button type="button" class="btn btn-primary w-100" onclick="addItemPI()">
              <i class="bi bi-plus-circle"></i> Add</button>
          </div>
        </div>
        <div id="specBoxPI" class="alert alert-secondary" style="display:none;font-size:12px">
          <div id="specTextPI" style="white-space:pre-line"></div>
        </div>
        <div class="table-responsive">
          <table class="table table-sm table-bordered">
            <thead style="background:#E2E8F0">
              <tr>
                <th style="width:5%">Sr.</th>
                <th style="width:20%">Item</th>
                <th style="width:30%">Specification</th>
                <th style="width:8%">Qty</th>
                <th style="width:8%">Unit</th>
                <th style="width:10%">Rate</th>
                <th style="width:12%">Amount</th>
                <th style="width:7%">Action</th>
              </tr>
            </thead>
            <tbody id="piItemsTbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="form-section" id="piTotalBox" style="display:none">
        <h5><i class="bi bi-calculator"></i> Total</h5>
        <div class="summary-box">
          <div class="total-line"><span>Sub-Total</span><b id="piSubtotal">Rs. 0</b></div>
          <div class="total-line"><span>GST (18%)</span><b id="piGST">Rs. 0</b></div>
          <div class="total-line grand-total"><span>Grand Total</span><b id="piGrand">Rs. 0</b></div>
        </div>
      </div>
      <div class="form-section">
        <button type="submit" class="btn btn-primary w-100 mb-2">
          <i class="bi bi-check-circle"></i> Update Proforma Invoice</button>
        <a href="/proforma/{pid}" class="btn btn-outline-secondary w-100">
          <i class="bi bi-x-circle"></i> Cancel</a>
      </div>
    </div>
  </div>
</form>
<script>{js}</script>'''
        return self._layout(f'Edit {p["pi_no"]}', f'Edit Proforma Invoice - {p["pi_no"]}', content, 'proforma')


    # ── PAGE: PROFORMA INVOICE DETAIL ─────────────────────────
    def pg_pidetail(self, pid):
        db = get_db()
        p = db.execute("SELECT * FROM proforma_invoices WHERE id=?", (pid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()
        c = None
        if p['customer_id']:
            c = db.execute("SELECT * FROM customers WHERE id=?",
                           (p['customer_id'],)).fetchone()
        items = db.execute(
            "SELECT * FROM proforma_invoice_items WHERE proforma_id=? "
            "ORDER BY sort_order", (pid,)).fetchall()
        db.close()

        subtotal = sum((it['quantity'] or 0) * (it['rate'] or 0) for it in items)
        gst = subtotal * 0.18
        grand = subtotal + gst

        item_rows = ''
        for i, it in enumerate(items, 1):
            amt = (it['quantity'] or 0) * (it['rate'] or 0)
            net = amt * (1 + (it['gst_percent'] or 18) / 100)
            spec = esc(it['specification'] or '').replace('&#10;', '<br>')
            item_rows += (f'<tr><td class="text-center"><strong>{i}</strong></td>'
                          f'<td><strong>{esc(it["item_name"])}</strong></td>'
                          f'<td style="font-size:11px">{esc_nl(it["description"] or "")}'
                          f'<br><br><b>Specification:</b><br>{spec}</td>'
                          f'<td class="text-center">{it["quantity"]}</td>'
                          f'<td class="text-center">{esc(it["unit"])}</td>'
                          f'<td class="text-end">{fmt_rupee(it["rate"])}</td>'
                          f'<td class="text-end">{fmt_rupee(amt)}</td>'
                          f'<td class="text-center">{it["gst_percent"]:.0f}%</td>'
                          f'<td class="text-end"><strong>{fmt_rupee(net)}</strong></td></tr>')
        if not items:
            item_rows = ('<tr><td colspan="9" class="text-center text-muted py-4">'
                         'No items in this proforma invoice.</td></tr>')

        cName = esc(c['contact_person']) if c else ''
        cCompany = esc(c['company_name']) if c else ''
        cAddr = esc_nl(c['address'] or '') if c else ''
        cGstin = esc(c['gstin'] or '') if c else ''

        status_opts = ''
        for s in ['Draft', 'Sent', 'Confirmed', 'Cancelled']:
            sel = ' selected' if p['status'] == s else ''
            status_opts += f'<option value="{s}"{sel}>{s}</option>'

        content = f'''
<div class="row g-3">
  <div class="col-lg-8">
    <div class="form-section" style="padding:32px;border:1px solid #CBD5E1">

      <div class="text-center mb-3">
        <h3 style="color:#DC2626;font-weight:800;letter-spacing:2px">AYPEE  INDUSTRIES</h3>
        <div class="d-flex justify-content-between" style="font-size:12px">
          <span><u>Subject to Jalandhar Jurisdiction</u></span>
          <span>GSTIN: 03AAHFA4106G1Z4</span>
        </div>
      </div>

      <table class="table table-bordered mb-2" style="font-size:11px">
        <tr>
          <td style="width:35%"><b>Correspondence Address:</b><br>
            STREET NO.1, ANAND NAGAR<br>NEAR KIDZ NEST SCHOOL,<br>
            MAKSUDAN, JALANDHAR (PUNJAB), INDIA<br>PIN CODE - 144008</td>
          <td style="width:27%;text-align:center;vertical-align:middle">
            <b style="color:#DC2626;font-size:14px">CHAINS</b><br>
            <i>MFG. OF ALL KINDS OF S.G. IRON CHAINS,<br>FABRICATED CANE CARRIER,<br>
            RBC &amp; RAKE CARRIER CHAINS,</i></td>
          <td style="width:38%;text-align:right;vertical-align:middle">
            <b>(Off.): 0181-2672260</b><br><br><b>(Mob): 9357255758, 9041881996</b></td>
        </tr>
      </table>

      <div class="text-center" style="font-size:10px;font-weight:700;border-top:1px solid #333;border-bottom:1px solid #333;padding:4px 0">
        MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, RBC &amp; RAKE CARRIER CHAINS, SLAT &amp; SPARE PARTS
      </div>

      <div class="d-flex justify-content-between mt-2" style="font-size:11px;border-bottom:1px solid #999;padding-bottom:4px">
        <span>E-mail: <u style="color:#0000CC">aypeeinds@yahoo.com</u></span>
        <b>Date: {esc(p["date"])}</b>
      </div>

      <div style="margin-top:16px"><b>To</b><br>
        <b>{cName}</b><br><b>{cCompany}</b><br>{cAddr}<br><small>{cGstin}</small>
      </div>

      <div class="d-flex justify-content-between align-items-center my-3">
        <b>Order No: {esc(p["order_no"] or "-")}</b>
        <b style="text-decoration:underline">PROFORMA INVOICE</b>
      </div>

      <table class="table table-bordered mt-3" style="font-size:12px">
        <thead style="background:#E2E8F0">
          <tr>
            <th style="width:5%">Sr.</th><th style="width:14%">Item</th>
            <th style="width:25%">Description &amp; Specification</th>
            <th style="width:7%">Qty.</th><th style="width:6%">Unit</th>
            <th style="width:9%">Rate</th><th style="width:10%">Amount</th>
            <th style="width:6%">GST%</th><th style="width:12%">Net Total</th>
          </tr>
        </thead>
        <tbody>{item_rows}</tbody>
      </table>

      <div class="text-end" style="font-size:13px">
        <div><b>Sub-Total: Rs. {fmt_rupee(subtotal)}</b></div>
        <div><b>GST (18%): Rs. {fmt_rupee(gst)}</b></div>
        <div style="font-size:17px;color:#DC2626;border-top:2px solid #333;padding-top:4px;margin-top:4px">
          <b>Grand Total: Rs. {fmt_rupee(grand)}</b></div>
      </div>

      <div style="margin-top:24px">
        <b>Thanks &amp; Regards</b><br><br>
        <b>For Aypee Industries</b><br><br><br>
        <b>Raman Budhia (Partner)</b><br>
        <b>+91-9357255758</b>
      </div>
    </div>
  </div>

  <div class="col-lg-4">
    <div class="form-section mb-3">
      <h5><i class="bi bi-file-text"></i> Details</h5>
      <table class="table table-sm mb-0">
        <tr><td class="text-muted">No</td><td class="text-end"><b>{esc(p["pi_no"])}</b></td></tr>
        <tr><td class="text-muted">Order No</td><td class="text-end">{esc(p["order_no"] or "-")}</td></tr>
        <tr><td class="text-muted">Date</td><td class="text-end">{esc(p["date"])}</td></tr>
        <tr><td class="text-muted">Status</td>
          <td class="text-end"><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td></tr>
        <tr><td class="text-muted">Customer</td><td class="text-end">{cCompany or "-"}</td></tr>
        <tr><td class="text-muted">Created</td><td class="text-end">{esc(p["created_at"] or "-")}</td></tr>
      </table>
    </div>

    <div class="form-section mb-3">
      <h5><i class="bi bi-calculator"></i> Financial Summary</h5>
      <div class="summary-box">
        <div class="total-line"><span>Sub-Total</span><b>Rs. {fmt_rupee(subtotal)}</b></div>
        <div class="total-line"><span>GST (18%)</span><b>Rs. {fmt_rupee(gst)}</b></div>
        <div class="total-line grand-total"><span>Grand Total</span><b>Rs. {fmt_rupee(grand)}</b></div>
      </div>
    </div>

    <div class="form-section">
      <h5><i class="bi bi-gear"></i> Actions</h5>
      <a href="/proforma/{p["id"]}/pdf" class="btn btn-danger w-100 mb-2">
        <i class="bi bi-file-earmark-pdf"></i> Download PDF</a>
      <a href="/proforma/{p["id"]}/edit" class="btn btn-primary w-100 mb-2">
        <i class="bi bi-pencil"></i> Edit Invoice</a>
      <form method="POST" action="/proforma/{p["id"]}/status" class="mb-2">
        <label class="form-label">Change Status</label>
        <select name="status" class="form-select form-select-sm" onchange="this.form.submit()">{status_opts}</select>
      </form>
      <a href="/proforma" class="btn btn-outline-primary w-100 mb-2">
        <i class="bi bi-arrow-left"></i> Back to List</a>
      <form method="POST" action="/proforma/{p["id"]}/delete"
            onsubmit="return confirm('Delete this proforma invoice permanently?')">
        <button class="btn btn-outline-danger w-100"><i class="bi bi-trash"></i> Delete</button>
      </form>
    </div>
  </div>
</div>'''
        return self._layout(p['pi_no'], p['pi_no'], content, 'proforma')


