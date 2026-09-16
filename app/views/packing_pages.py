import json
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import redirect
from utils.http_utils import pdf_response
from database import get_db
from database import gen_packing_list_no
from datetime import datetime
import math


class PackingPagesMixin:
    PACKING_STATUSES = ['Draft', 'Sent', 'Confirmed', 'Cancelled']
    PACKING_PAGE_SIZE = 10

    # ── PAGE: PACKING LIST ─────────────────────────────────────
    def _packing_rows_html(self, rows_data):
        """Render <tr> rows for a set of packing list records (already fetched)."""
        rows = ''
        for p in rows_data:
            rows += (f'<tr><td><strong>{esc(p["pl_no"])}</strong></td>'
                     f'<td>{esc(p["date"])}</td>'
                     f'<td>{esc(p["contact_person"] or "-")}</td>'
                     f'<td>{esc(p["company_name"] or "-")}</td>'
                     f'<td>{esc(p["subject"] or "-")}</td>'
                     f'<td><span class="badge badge-{esc(p["status"].lower())}">{esc(p["status"])}</span></td>'
                     f'<td class="text-end">'
                     f'<a href="/packing/{p["id"]}" class="btn btn-sm btn-outline-primary me-1" title="View"><i class="bi bi-eye"></i></a>'
                     f'<a href="/packing/{p["id"]}/pdf" class="btn btn-sm btn-outline-danger" title="Download PDF"><i class="bi bi-file-earmark-pdf"></i></a>'
                     f'<form method="POST" action="/packing/{p["id"]}/delete" class="d-inline" onsubmit="return confirm(\'Delete this packing list?\')">'
                     f'<button class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button></form>'
                     f'</td></tr>')
        return rows

    def _packing_empty_state(self, has_any, q, status):
        """Illustration + message for zero results, whether the table is
        truly empty or just has no matches for the current search/filter."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-box-seam"></i></div>'
                    '<h6>No packing lists yet</h6>'
                    '<p class="text-muted">Once you create a packing list, it will show up here.</p>'
                    '<a href="/packing/new" class="btn btn-primary btn-sm">'
                    '<i class="bi bi-plus-circle"></i> Create your first packing list</a>'
                    '</div>')
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching packing lists</h6>'
                '<p class="text-muted">Try a different search term'
                + (f' or clear the "{esc(status)}" filter' if status else '')
                + '.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="plClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _packing_pagination_html(self, page, total_pages, q, status):
        if total_pages <= 1:
            return ''
        html = '<nav aria-label="Packing pagination"><ul class="pagination pagination-sm q-pagination justify-content-end mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="plGoToPage(event,{p})">{label}</a></li>')

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

    def _packing_chips_html(self, status_counts, all_count, status):
        chips = [('', 'All', all_count)] + [(s, s, status_counts.get(s, 0)) for s in self.PACKING_STATUSES]
        html = ''
        for val, label, count in chips:
            active = ' active' if val == status else ''
            html += (f'<button type="button" class="q-chip{active}" data-status="{esc(val)}" '
                     f'onclick="plSetStatus(\'{esc(val)}\')">{esc(label)} '
                     f'<span class="q-chip-count">{count}</span></button>')
        return html

    def _packing_fragment(self, q='', status='', page='1'):
        """Fetch, filter, and paginate packing lists; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/filter/pagination without a full reload."""
        q = (q or '').strip()
        status = status or ''
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        if status not in self.PACKING_STATUSES:
            status = ''

        db = get_db()

        search_where, search_params = [], []
        if q:
            like = f"%{q}%"
            search_where.append(
                "(p.pl_no LIKE ? OR c.company_name LIKE ? OR c.contact_person LIKE ? OR p.date LIKE ?)")
            search_params.extend([like, like, like, like])
        search_sql = (" WHERE " + " AND ".join(search_where)) if search_where else ""

        has_any = db.execute("SELECT COUNT(*) c FROM packing_lists").fetchone()['c'] > 0

        all_count = db.execute(
            f"SELECT COUNT(*) c FROM packing_lists p LEFT JOIN customers c ON p.customer_id=c.id{search_sql}",
            search_params).fetchone()['c']
        status_counts = {}
        for s in self.PACKING_STATUSES:
            join = " AND" if search_where else " WHERE"
            r = db.execute(
                f"SELECT COUNT(*) c FROM packing_lists p LEFT JOIN customers c ON p.customer_id=c.id{search_sql}{join} p.status=?",
                search_params + [s]).fetchone()
            status_counts[s] = r['c']

        where = list(search_where)
        params = list(search_params)
        if status:
            where.append("p.status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = db.execute(
            f"SELECT COUNT(*) c FROM packing_lists p LEFT JOIN customers c ON p.customer_id=c.id{where_sql}",
            params).fetchone()['c']
        total_pages = max(1, math.ceil(total / self.PACKING_PAGE_SIZE))
        page = min(page, total_pages)
        offset = (page - 1) * self.PACKING_PAGE_SIZE

        rows_data = db.execute(
            f"SELECT p.*,c.company_name,c.contact_person FROM packing_lists p "
            f"LEFT JOIN customers c ON p.customer_id=c.id{where_sql} "
            f"ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            params + [self.PACKING_PAGE_SIZE, offset]).fetchall()
        db.close()

        if rows_data:
            rows_html = self._packing_rows_html(rows_data)
        else:
            rows_html = f'<tr><td colspan="7" class="p-0">{self._packing_empty_state(has_any, q, status)}</td></tr>'

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + self.PACKING_PAGE_SIZE, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total}'
                       if total else 'No packing lists found')

        return {
            'rows_html': rows_html,
            'pagination_html': self._packing_pagination_html(page, total_pages, q, status),
            'chips_html': self._packing_chips_html(status_counts, all_count, status),
            'count_label': count_label,
            'total': total,
            'page': page,
            'total_pages': total_pages,
        }

    def pg_pllist(self, q='', status='', page='1'):
        frag = self._packing_fragment(q=q, status=status, page=page)
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
#packingTbody {{ transition:opacity .1s; }}
#packingTbody.q-loading {{ opacity:.4; }}
.q-pagination .page-link {{ color:var(--primary); }}
.q-pagination .page-item.active .page-link {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-pagination .page-item.disabled .page-link {{ color:#94A3B8; }}
</style>

<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-box-seam"></i> Packing Lists (<span id="plHeaderCount">{frag["total"]}</span>)</h5>
  <a href="/packing/new" class="btn btn-primary btn-sm"><i class="bi bi-plus-circle"></i> New Packing List</a>
</div>

<div class="table-card">
  <div class="p-3 border-bottom">
    <div class="q-toolbar">
      <div class="input-group input-group-sm" style="max-width:320px">
        <span class="input-group-text bg-white border-end-0"><i class="bi bi-search text-muted"></i></span>
        <input type="text" id="plSearchInput" class="form-control border-start-0" placeholder="Search by no., customer, or date" value="{esc(q)}">
      </div>
      <div class="q-chips" id="plChips">{frag["chips_html"]}</div>
    </div>
  </div>
  <table class="table mb-0">
    <thead><tr><th>No</th><th>Date</th><th>Contact</th><th>Company</th><th>Subject</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
    <tbody id="packingTbody">{frag["rows_html"]}</tbody>
  </table>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="plCountLabel">{frag["count_label"]}</span>
    <div id="plPagination">{frag["pagination_html"]}</div>
  </div>
</div>

<script>
(function() {{
  var state = {{ q: {json.dumps(q)}, status: {json.dumps(status)}, page: {frag["page"]} }};
  var debounceTimer = null;

  function refresh() {{
    var tbody = document.getElementById('packingTbody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    params.set('page', state.page);
    fetch('/api/packing?' + params.toString())
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('plPagination').innerHTML = data.pagination_html;
        document.getElementById('plChips').innerHTML = data.chips_html;
        document.getElementById('plCountLabel').innerHTML = data.count_label;
        document.getElementById('plHeaderCount').textContent = data.total;
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.status) url.searchParams.set('status', state.status); else url.searchParams.delete('status');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function() {{ tbody.classList.remove('q-loading'); }});
  }}

  window.plGoToPage = function(e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.plSetStatus = function(s) {{
    state.status = s;
    state.page = 1;
    refresh();
  }};

  window.plClearFilters = function() {{
    state.q = '';
    state.status = '';
    state.page = 1;
    document.getElementById('plSearchInput').value = '';
    refresh();
  }};

  var searchInput = document.getElementById('plSearchInput');
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
        return self._layout('Packing Lists', 'All Packing Lists', content, 'packing')


    def pg_plnew(self):
        db = get_db()
        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        # Fetch all proforma invoices for the dropdown
        profs = db.execute(
            "SELECT pi.*, c.company_name FROM proforma_invoices pi "
            "LEFT JOIN customers c ON pi.customer_id = c.id "
            "ORDER BY pi.created_at DESC").fetchall()
        plno = gen_packing_list_no(db)
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
                          f'data-unit="{esc(i["unit"])}">'
                          f'{esc(i["item_name"])} ({esc(i["unit"])})</option>')

        prof_opts = '<option value="">- Select Proforma Invoice -</option>'
        for p in profs:
            label = f'{p["pi_no"]}'
            if p['company_name']:
                label += f' ({p["company_name"]})'
            label += f' - {p["date"]}'
            prof_opts += f'<option value="{p["id"]}" data-customer="{p["customer_id"] or ""}">{esc(label)}</option>'

        js = """
let plItems = [];
let plCounter = 0;

function fillCustPL() {
  const sel = document.getElementById('custSelPL');
  const info = document.getElementById('custInfoPL');
  if (sel.value) {
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePL').textContent = opt.dataset.name;
    document.getElementById('cCompanyPL').textContent = opt.dataset.company;
    document.getElementById('cAddrPL').textContent = opt.dataset.address;
    info.style.display = 'block';
  } else {
    info.style.display = 'none';
  }
}

function loadProformaItems() {
  const sel = document.getElementById('profSelPL');
  const piId = sel.value;
  const btn = document.getElementById('btnLoadProf');
  const msg = document.getElementById('profMsg');
  if (!piId) { msg.textContent = ''; return; }
  msg.innerHTML = '<span class="text-muted"><i class="bi bi-arrow-clockwise"></i> Loading items...</span>';
  // Store pi_id in hidden field
  document.getElementById('piIdPL').value = piId;
  fetch('/api/proforma/' + piId + '/items')
    .then(r => r.json())
    .then(data => {
      if (data.items && data.items.length > 0) {
        // clear current items
        plItems = [];
        data.items.forEach(function(it) {
          plCounter++;
          plItems.push({
            id: plCounter,
            itemId: it.item_id || '',
            name: it.item_name || '',
            desc: it.description || '',
            spec: it.specification || '',
            unit: it.unit || 'Pcs',
            qty: it.quantity || 0,
            bag: 0
          });
        });
        // auto-select customer from proforma
        if (data.customer_id) {
          document.getElementById('custSelPL').value = data.customer_id;
          fillCustPL();
        }
        renderItemsPL();
        msg.innerHTML = '<span class="text-success"><i class="bi bi-check-circle"></i> ' + data.items.length + ' items loaded from proforma. Enter Qty/Bag for each item below.</span>';
      } else {
        msg.innerHTML = '<span class="text-warning">No items found in this proforma invoice.</span>';
      }
    })
    .catch(err => {
      msg.innerHTML = '<span class="text-danger">Error loading items. Please try again.</span>';
    });
}

function addItemPL() {
  const sel = document.getElementById('itemSelPL');
  const qty = parseFloat(document.getElementById('itemQtyPL').value);
  const bag = parseFloat(document.getElementById('itemBagPL').value);
  if (!sel.value) { alert('Please select an item.'); return; }
  if (!qty || qty <= 0) { alert('Please enter total quantity.'); return; }
  if (!bag || bag <= 0) { alert('Please enter quantity per bag.'); return; }
  const opt = sel.options[sel.selectedIndex];
  plCounter++;
  plItems.push({
    id: plCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc,
    spec: opt.dataset.spec,
    unit: opt.dataset.unit,
    qty: qty,
    bag: bag
  });
  renderItemsPL();
  sel.value = '';
  document.getElementById('itemQtyPL').value = '';
  document.getElementById('itemBagPL').value = '';
}

function removeItemPL(id) {
  plItems = plItems.filter(function(i) { return i.id !== id; });
  renderItemsPL();
}

function updateBagQty(id, val) {
  const item = plItems.find(function(i) { return i.id === id; });
  if (item) {
    item.bag = parseFloat(val) || 0;
    // Update hidden input and display text without full re-render
    const hidden = document.querySelector('input[name="bag_qty[]"][value="' + 0 + '"]');
    // Update hidden inputs for this item
    document.querySelectorAll('#itemsListPL .quotation-item-row').forEach(function(row, idx) {
      if (plItems[idx] && plItems[idx].id === id) {
        const hiddens = row.querySelectorAll('input[type="hidden"]');
        if (hiddens[2]) hiddens[2].value = item.bag;
        const infoDiv = row.querySelector('small.text-muted:last-of-type, small.text-warning');
        if (infoDiv) {
          const totalBags = item.bag > 0 ? Math.ceil(item.qty / item.bag) : 0;
          const fullBags = item.bag > 0 ? Math.floor(item.qty / item.bag) : 0;
          const lastBag = item.bag > 0 ? (item.qty - fullBags * item.bag) : 0;
          let breakdown = '';
          if (item.bag > 0 && item.qty > 0) {
            if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
            if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
            else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
            if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
          }
          if (item.bag > 0) {
            infoDiv.className = 'text-muted';
            infoDiv.innerHTML = 'Bags: <strong>' + totalBags + '</strong>' + (breakdown ? ' (' + breakdown + ')' : '');
          } else {
            infoDiv.className = 'text-warning';
            infoDiv.textContent = 'Enter Qty/Bag';
          }
        }
      }
    });
  }
}

function updateTotalQty(id, val) {
  const item = plItems.find(function(i) { return i.id === id; });
  if (item) {
    item.qty = parseFloat(val) || 0;
    // Update hidden input for quantity
    document.querySelectorAll('#itemsListPL .quotation-item-row').forEach(function(row, idx) {
      if (plItems[idx] && plItems[idx].id === id) {
        const hiddens = row.querySelectorAll('input[type="hidden"]');
        if (hiddens[1]) hiddens[1].value = item.qty;
        const infoDiv = row.querySelector('small.text-muted:last-of-type, small.text-warning');
        if (infoDiv && item.bag > 0) {
          const totalBags = Math.ceil(item.qty / item.bag);
          const fullBags = Math.floor(item.qty / item.bag);
          const lastBag = item.qty - fullBags * item.bag;
          let breakdown = '';
          if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
          if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
          else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
          if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
          infoDiv.innerHTML = 'Bags: <strong>' + totalBags + '</strong> (' + breakdown + ')';
        }
      }
    });
  }
}

function renderItemsPL() {
  const list = document.getElementById('itemsListPL');
  if (plItems.length === 0) {
    list.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><p>No items added yet.</p></div>';
    return;
  }
  let html = '';
  plItems.forEach(function(item, idx) {
    const totalBags = item.bag > 0 ? Math.ceil(item.qty / item.bag) : 0;
    const fullBags = item.bag > 0 ? Math.floor(item.qty / item.bag) : 0;
    const lastBag = item.bag > 0 ? (item.qty - fullBags * item.bag) : 0;
    let breakdown = '';
    if (item.bag > 0 && item.qty > 0) {
      if (fullBags > 0) breakdown += fullBags + ' x ' + item.bag;
      if (lastBag > 0 && lastBag !== item.bag) breakdown += (breakdown ? ' + ' : '') + '1 x ' + lastBag;
      else if (lastBag > 0 && fullBags === 0) breakdown += '1 x ' + lastBag;
      if (!breakdown && item.qty > 0) breakdown = '1 x ' + item.qty;
    }
    html += `
    <div class="quotation-item-row">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1">
          <span class="item-name">${idx + 1}. ${item.name}</span>
          <input type="hidden" name="item_id[]" value="${item.itemId}">
          <input type="hidden" name="quantity[]" value="${item.qty}">
          <input type="hidden" name="bag_qty[]" value="${item.bag}">
          <div class="item-spec">${(item.spec || '').replace(/\\n/g, '<br>')}</div>
          <div class="row g-1 mt-1 align-items-center">
            <div class="col-auto"><small class="text-muted">Total Qty:</small></div>
            <div class="col-auto"><input type="number" step="0.01" min="0" class="form-control form-control-sm" style="width:90px" value="${item.qty}" onchange="updateTotalQty(${item.id}, this.value)"></div>
            <div class="col-auto"><small class="text-muted">${item.unit}</small></div>
            <div class="col-auto"><small class="text-muted">|</small></div>
            <div class="col-auto"><small class="text-muted">Qty/Bag:</small></div>
            <div class="col-auto"><input type="number" step="0.01" min="0" class="form-control form-control-sm" style="width:90px" value="${item.bag}" placeholder="0" onchange="updateBagQty(${item.id}, this.value)"></div>
            <div class="col-auto"><small class="text-muted">${item.unit}</small></div>
          </div>
          ${item.bag > 0 ? `<small class="text-muted">Bags: <strong>${totalBags}</strong> ${breakdown ? '(' + breakdown + ')' : ''}</small>` : '<small class="text-warning">Enter Qty/Bag</small>'}
        </div>
        <div class="text-end">
          <button type="button" class="btn btn-sm btn-outline-danger mt-1" onclick="removeItemPL(${item.id})"><i class="bi bi-x-circle"></i> Remove</button>
        </div>
      </div>
    </div>`;
  });
  list.innerHTML = html;
}

function submitFormPL() {
  // Validate that all items have bag qty set
  for (let i = 0; i < plItems.length; i++) {
    if (!plItems[i].bag || plItems[i].bag <= 0) {
      alert('Please enter Qty/Bag for item: ' + plItems[i].name);
      return false;
    }
    if (!plItems[i].qty || plItems[i].qty <= 0) {
      alert('Please enter Total Qty for item: ' + plItems[i].name);
      return false;
    }
  }
  if (plItems.length === 0) {
    alert('Please add at least one item to the packing list.');
    return false;
  }
  return true;
}
"""

        content = f'''
<form method="POST" action="/packing/new" onsubmit="return submitFormPL()">
  <div class="row g-3">
    <div class="col-lg-8">
      <div class="form-section mb-3">
        <h5><i class="bi bi-info-circle"></i> Packing List Info</h5>
        <div class="row g-2">
          <div class="col-md-4"><label class="form-label">PL No</label>
            <input class="form-control" value="{esc(plno)}" readonly style="background:#F8FAFC"></div>
          <div class="col-md-4"><label class="form-label">Date</label>
            <input name="date" class="form-control" value="{esc(today)}"></div>
          <div class="col-md-4"><label class="form-label">Status</label>
            <input class="form-control" value="Draft" readonly style="background:#F8FAFC"></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-file-earmark-text"></i> Import from Proforma Invoice (Optional)</h5>
        <div class="row g-2">
          <div class="col-md-8"><label class="form-label">Select Proforma Invoice</label>
            <select class="form-select" id="profSelPL">{prof_opts}</select></div>
          <div class="col-md-4"><label class="form-label">&nbsp;</label>
            <button type="button" class="btn btn-success w-100" id="btnLoadProf" onclick="loadProformaItems()"><i class="bi bi-download"></i> Import Items</button></div>
        </div>
        <div id="profMsg" class="mt-2" style="font-size:13px"></div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-person"></i> Customer</h5>
        <select name="customer_id" class="form-select" id="custSelPL" onchange="fillCustPL()">{cust_opts}</select>
        <div id="custInfoPL" class="alert alert-info mt-3" style="display:none">
          <strong id="cNamePL"></strong><br>
          <span id="cCompanyPL"></span><br>
          <span id="cAddrPL" style="white-space:pre-line"></span>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-cart-plus"></i> Add Item Manually</h5>
        <div class="row g-2 mb-3">
          <div class="col-md-4"><label class="form-label">Select Item</label>
            <select class="form-select" id="itemSelPL">{item_opts}</select></div>
          <div class="col-md-3"><label class="form-label">Total Qty</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemQtyPL" placeholder="0"></div>
          <div class="col-md-3"><label class="form-label">Qty / Bag</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemBagPL" placeholder="0"></div>
          <div class="col-md-2"><label class="form-label">&nbsp;</label>
            <button type="button" class="btn btn-primary w-100" onclick="addItemPL()"><i class="bi bi-plus"></i> Add</button></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-list-ol"></i> Items in Packing List</h5>
        <div id="itemsListPL">
          <div class="empty-state"><i class="bi bi-inbox"></i><p>No items added yet. Import from a proforma invoice or add manually above.</p></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-text-left"></i> Notes</h5>
        <textarea name="notes" class="form-control" rows="3" placeholder="Optional notes"></textarea>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="form-section">
        <input type="hidden" name="pi_id" id="piIdPL" value="">
        <button type="submit" class="btn btn-primary w-100 mb-2"><i class="bi bi-check-circle"></i> Create Packing List</button>
        <a href="/packing" class="btn btn-outline-secondary w-100"><i class="bi bi-x-circle"></i> Cancel</a>
      </div>
    </div>
  </div>
</form>
<script>{js}</script>'''
        return self._layout(f'New {plno}', 'Create Packing List', content, 'packing')


    def handle_packing_create(self, form, user_id):
        db = get_db()
        plno = gen_packing_list_no(db)
        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None
        try:
            pi_id = int(form.get('pi_id')) if form.get('pi_id') else None
        except (TypeError, ValueError):
            pi_id = None
        cur = db.execute(
            "INSERT INTO packing_lists "
            "(pl_no,pi_id,date,customer_id,subject,status,notes,created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (plno, pi_id, date, customer_id, form.get('subject', ''),
             'Draft', form.get('notes', ''), user_id))
        plid = cur.lastrowid

        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        bag_qtys = form.getlist('bag_qty[]')
        for i, (iid, qty, bagq) in enumerate(zip(item_ids, quantities, bag_qtys)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
                bagq = float(bagq) if bagq else qty
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO packing_list_items "
                    "(packing_list_id,item_id,item_name,description,specification,"
                    "total_qty,unit,bag_qty,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (plid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'], bagq, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/packing/{plid}'))


    def handle_packing_edit(self, plid, form, user_id):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.send(*redirect('/packing'))

        date = form.get('date', datetime.now().strftime('%d - %m - %Y'))
        try:
            customer_id = int(form.get('customer_id')) if form.get('customer_id') else None
        except (TypeError, ValueError):
            customer_id = None

        db.execute(
            "UPDATE packing_lists SET date=?, customer_id=?, "
            "subject=?, notes=? WHERE id=?",
            (date, customer_id, form.get('subject', ''),
             form.get('notes', ''), plid))

        db.execute("DELETE FROM packing_list_items WHERE packing_list_id=?", (plid,))
        item_ids = form.getlist('item_id[]')
        quantities = form.getlist('quantity[]')
        bag_qtys = form.getlist('bag_qty[]')
        for i, (iid, qty, bagq) in enumerate(zip(item_ids, quantities, bag_qtys)):
            if not iid or not qty:
                continue
            try:
                iid = int(iid)
                qty = float(qty)
                bagq = float(bagq) if bagq else qty
            except (TypeError, ValueError):
                continue
            item = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if item:
                db.execute(
                    "INSERT INTO packing_list_items "
                    "(packing_list_id,item_id,item_name,description,specification,"
                    "total_qty,unit,bag_qty,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (plid, iid, item['item_name'], item['description'],
                     item['specification'], qty, item['unit'], bagq, i))
        db.commit()
        db.close()
        return self.send(*redirect(f'/packing/{plid}'))


    def pg_pldetail(self, plid):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()
        c = None
        if p['customer_id']:
            c = db.execute("SELECT * FROM customers WHERE id=?",
                           (p['customer_id'],)).fetchone()
        pi = None
        if p['pi_id']:
            pi = db.execute("SELECT pi_no FROM proforma_invoices WHERE id=?",
                            (p['pi_id'],)).fetchone()
        items = db.execute(
            "SELECT * FROM packing_list_items WHERE packing_list_id=? "
            "ORDER BY sort_order", (plid,)).fetchall()
        db.close()

        item_rows = ''
        for idx, it in enumerate(items, 1):
            total_qty = it['total_qty'] or 0
            bag_qty = it['bag_qty'] or 0
            total_bags = math.ceil(total_qty / bag_qty) if bag_qty > 0 else 1
            last_bag_qty = total_qty - (bag_qty * (total_bags - 1)) if total_bags > 1 else total_qty
            item_rows += (f'<tr><td class="text-center"><strong>{idx}</strong></td>'
                          f'<td><strong>{esc(it["item_name"])}</strong><br>'
                          f'<small class="text-muted" style="white-space:pre-line">{esc_nl(it["description"] or "")}</small></td>'
                          f'<td class="text-center">{it["total_qty"]}</td>'
                          f'<td class="text-center">{esc(it["unit"])}</td>'
                          f'<td class="text-center">{it["bag_qty"]}</td>'
                          f'<td class="text-center"><strong>{total_bags}</strong></td>'
                          f'<td class="text-center">{last_bag_qty}</td></tr>')
        if not items:
            item_rows = ('<tr><td colspan="7" class="text-center text-muted py-4">'
                         'No items in this packing list.</td></tr>')

        cName = esc(c['contact_person']) if c else ''
        cCompany = esc(c['company_name']) if c else ''
        cAddr = esc_nl(c['address'] or '') if c else ''
        piLink = ''
        if pi:
            piLink = (f'<div class="mb-2"><small class="text-muted">Linked Proforma: '
                      f'<a href="/proforma/{p["pi_id"]}" style="text-decoration:none">'
                      f'<strong>{esc(pi["pi_no"])}</strong></a></small></div>')

        status_opts = ''
        for s in ['Draft', 'Sent', 'Confirmed', 'Cancelled']:
            sel = ' selected' if p['status'] == s else ''
            status_opts += f'<option value="{s}"{sel}>{s}</option>'

        content = f'''
<div class="d-flex justify-content-between align-items-center mb-3">
  <div>
    <h5 class="mb-0"><i class="bi bi-box-seam"></i> {esc(p['pl_no'])}</h5>
    <small class="text-muted">Created {esc(p['date'])}</small>
  </div>
  <div>
    <a href="/packing/{plid}/pdf" class="btn btn-danger btn-sm me-1"><i class="bi bi-file-earmark-pdf"></i> PDF</a>
    <a href="/packing/{plid}/edit" class="btn btn-primary btn-sm me-1"><i class="bi bi-pencil"></i> Edit</a>
    <form method="POST" action="/packing/{plid}/delete" class="d-inline" onsubmit="return confirm(\'Delete this packing list?\')">
      <button class="btn btn-outline-danger btn-sm"><i class="bi bi-trash"></i></button>
    </form>
  </div>
</div>

<div class="form-section mb-3">
  <div class="row">
    <div class="col-md-8">
      <h6>Customer</h6>
      {piLink}
      <p class="mb-0"><strong>{cCompany}</strong><br>
      {cName}<br>{cAddr}</p>
    </div>
    <div class="col-md-4">
      <h6>Status</h6>
      <form method="POST" action="/packing/{plid}/status" class="d-inline">
        <select name="status" class="form-select form-select-sm d-inline-block w-auto" onchange="this.form.submit()">
          {status_opts}
        </select>
      </form>
    </div>
  </div>
</div>

<div class="form-section">
  <h5><i class="bi bi-list-ol"></i> Packing Items</h5>
  <div class="table-responsive">
    <table class="table table-bordered">
      <thead style="background:#E2E8F0">
        <tr><th>Sr.</th><th>Item</th><th>Total Qty</th><th>Unit</th><th>Qty/Bag</th><th>Bags</th><th>Last Bag</th></tr>
      </thead>
      <tbody>{item_rows}</tbody>
    </table>
  </div>
  {('<div class="mt-2"><strong>Notes:</strong> ' + esc(p['notes'] or '') + '</div>') if p['notes'] else ''}
</div>'''
        return self._layout(f'{esc(p["pl_no"])}', f'Packing List - {p["pl_no"]}', content, 'packing')


    def pg_pledit(self, plid):
        db = get_db()
        p = db.execute("SELECT * FROM packing_lists WHERE id=?", (plid,)).fetchone()
        if not p:
            db.close()
            return self.pg_404()

        customers = db.execute("SELECT * FROM customers ORDER BY company_name").fetchall()
        items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        pl_items = db.execute(
            "SELECT * FROM packing_list_items WHERE packing_list_id=? ORDER BY sort_order",
            (plid,)).fetchall()
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
                          f'data-unit="{esc(i["unit"])}">'
                          f'{esc(i["item_name"])} ({esc(i["unit"])})</option>')

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
        for idx, it in enumerate(pl_items):
            if idx > 0:
                existing_items_js += ','
            existing_items_js += '{'
            existing_items_js += f'id: {idx + 1}, '
            existing_items_js += f'itemId: {it["item_id"]}, '
            existing_items_js += f'name: "{_js_str(it["item_name"])}", '
            existing_items_js += f'desc: "{_js_str(it["description"])}", '
            existing_items_js += f'spec: "{_js_str(it["specification"])}", '
            existing_items_js += f'unit: "{_js_str(it["unit"])}", '
            existing_items_js += f'qty: {it["total_qty"]}, '
            existing_items_js += f'bag: {it["bag_qty"]}'
            existing_items_js += '}'
        existing_items_js += ']'

        js = f"""
let plItems = {existing_items_js};
let plCounter = {len(pl_items)};

function fillCustPL() {{
  const sel = document.getElementById('custSelPL');
  const info = document.getElementById('custInfoPL');
  if (sel.value) {{
    const opt = sel.options[sel.selectedIndex];
    document.getElementById('cNamePL').textContent = opt.dataset.name;
    document.getElementById('cCompanyPL').textContent = opt.dataset.company;
    document.getElementById('cAddrPL').textContent = opt.dataset.address;
    info.style.display = 'block';
  }} else {{
    info.style.display = 'none';
  }}
}}

function addItemPL() {{
  const sel = document.getElementById('itemSelPL');
  const qty = parseFloat(document.getElementById('itemQtyPL').value);
  const bag = parseFloat(document.getElementById('itemBagPL').value);
  if (!sel.value) {{ alert('Please select an item.'); return; }}
  if (!qty || qty <= 0) {{ alert('Please enter total quantity.'); return; }}
  if (!bag || bag <= 0) {{ alert('Please enter quantity per bag.'); return; }}
  const opt = sel.options[sel.selectedIndex];
  plCounter++;
  plItems.push({{
    id: plCounter,
    itemId: sel.value,
    name: opt.dataset.name,
    desc: opt.dataset.desc,
    spec: opt.dataset.spec,
    unit: opt.dataset.unit,
    qty: qty,
    bag: bag
  }});
  renderItemsPL();
  sel.value = '';
  document.getElementById('itemQtyPL').value = '';
  document.getElementById('itemBagPL').value = '';
}}

function removeItemPL(id) {{
  plItems = plItems.filter(function(i) {{ return i.id !== id; }});
  renderItemsPL();
}}

function renderItemsPL() {{
  const list = document.getElementById('itemsListPL');
  if (plItems.length === 0) {{
    list.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><p>No items added yet.</p></div>';
    return;
  }}
  let html = '';
  plItems.forEach(function(item, idx) {{
    const totalBags = Math.ceil(item.qty / item.bag);
    html += `
    <div class="quotation-item-row">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="item-name">${{idx + 1}}. ${{item.name}}</span>
          <input type="hidden" name="item_id[]" value="${{item.itemId}}">
          <input type="hidden" name="quantity[]" value="${{item.qty}}">
          <input type="hidden" name="bag_qty[]" value="${{item.bag}}">
          <div class="item-spec">${{item.spec.replace(/\\n/g, '<br>')}}</div>
          <small class="text-muted">Total: ${{item.qty}} ${{item.unit}} | Per Bag: ${{item.bag}} ${{item.unit}} | Bags: ${{totalBags}}</small>
        </div>
        <div class="text-end">
          <button type="button" class="btn btn-sm btn-outline-danger mt-1" onclick="removeItemPL(${{item.id}})"><i class="bi bi-x-circle"></i> Remove</button>
        </div>
      </div>
    </div>`;
  }});
  list.innerHTML = html;
}}

function submitFormPL() {{
  if (plItems.length === 0) {{
    alert('Please add at least one item to the packing list.');
    return false;
  }}
  return true;
}}

window.addEventListener('DOMContentLoaded', function() {{ renderItemsPL(); fillCustPL(); }});
"""

        content = f'''
<form method="POST" action="/packing/{plid}/edit" onsubmit="return submitFormPL()">
  <div class="row g-3">
    <div class="col-lg-8">
      <div class="form-section mb-3">
        <h5><i class="bi bi-info-circle"></i> Edit Packing List</h5>
        <div class="row g-3">
          <div class="col-md-4"><label class="form-label">PL No.</label>
            <input type="text" class="form-control" value="{esc(p['pl_no'])}" readonly></div>
          <div class="col-md-4"><label class="form-label">Date</label>
            <input name="date" class="form-control" value="{esc(p['date'])}"></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-person"></i> Customer</h5>
        <select name="customer_id" class="form-select" id="custSelPL" onchange="fillCustPL()">{cust_opts}</select>
        <div id="custInfoPL" class="alert alert-info mt-3" style="display:{'block' if p['customer_id'] else 'none'}">
          <strong id="cNamePL"></strong><br>
          <span id="cCompanyPL"></span><br>
          <span id="cAddrPL" style="white-space:pre-line"></span>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-cart-plus"></i> Add Items</h5>
        <div class="row g-2 mb-3">
          <div class="col-md-4"><label class="form-label">Select Item</label>
            <select class="form-select" id="itemSelPL">{item_opts}</select></div>
          <div class="col-md-3"><label class="form-label">Total Qty</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemQtyPL" placeholder="0"></div>
          <div class="col-md-3"><label class="form-label">Qty / Bag</label>
            <input type="number" step="0.01" min="0" class="form-control" id="itemBagPL" placeholder="0"></div>
          <div class="col-md-2"><label class="form-label">&nbsp;</label>
            <button type="button" class="btn btn-primary w-100" onclick="addItemPL()"><i class="bi bi-plus"></i> Add</button></div>
        </div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-list-ol"></i> Items in Packing List</h5>
        <div id="itemsListPL"></div>
      </div>

      <div class="form-section mb-3">
        <h5><i class="bi bi-text-left"></i> Notes</h5>
        <textarea name="notes" class="form-control" rows="3" placeholder="Optional notes">{esc(p['notes'] or '')}</textarea>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="form-section">
        <button type="submit" class="btn btn-primary w-100 mb-2"><i class="bi bi-check-circle"></i> Update Packing List</button>
        <a href="/packing/{plid}" class="btn btn-outline-secondary w-100"><i class="bi bi-x-circle"></i> Cancel</a>
      </div>
    </div>
  </div>
</form>
<script>{js}</script>'''
        return self._layout(f'Edit {p["pl_no"]}', f'Edit Packing List - {p["pl_no"]}', content, 'packing')


    def _gen_pl_pdf(self, plid):
        from app.generators.packing_generator import build_packing_pdf
        db = get_db()
        row = db.execute("SELECT * FROM packing_lists WHERE id=?",
                         (plid,)).fetchone()
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
            "SELECT * FROM packing_list_items WHERE packing_list_id=? "
            "ORDER BY sort_order", (plid,)).fetchall()]
        db.close()
        pdf = build_packing_pdf(p, c, items)
        return pdf_response(pdf, f"{p['pl_no']}.pdf")


