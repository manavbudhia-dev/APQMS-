import json
import math
import re
from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from utils.http_utils import json_response
from utils.http_utils import json_for_script
from database import get_db

# Matches **bold text** so it can be rendered as <strong> wherever a
# description/specification is displayed. Applied AFTER html-escaping the
# raw text, so the ** markers themselves can't be used to inject markup.
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)


def render_formatted(text):
    """Escape HTML in `text`, then turn **word** into <strong>word</strong>.

    Use this (instead of esc/esc_nl) anywhere a stored description or
    specification is rendered as HTML, so the bold markers users type in
    the textarea actually show up bold.
    """
    escaped = esc(text or '')
    return _BOLD_RE.sub(r'<strong>\1</strong>', escaped)


# Item "type" (Chains / Bushes / Pins / Rollers / Others) is inferred
# from the item name — there's no dedicated column for it in the
# schema, and it's a different, lighter-weight thing from the real
# many-to-many "Categories" feature (item_categories table) used
# elsewhere. It only powers the filter tabs on the catalog page.
#
# The list order is a priority order: an item belongs to the FIRST
# bucket whose keyword appears in its name (so "PIN FOR CANE CARRIER
# CHAIN" still counts as a Pin, not a Chain, because 'pin' is checked
# first). Anything matching none of them falls into "Others".
ITEM_TYPES = [
    ('pins', 'Pins', 'pin'),
    ('bushes', 'Bushes', 'bush'),
    ('rollers', 'Rollers', 'roller'),
    ('chains', 'Chains', 'chain'),
]
TYPE_KEYS = [k for k, _, _ in ITEM_TYPES] + ['others']
TYPE_LABELS = {k: l for k, l, _ in ITEM_TYPES}
TYPE_LABELS['others'] = 'Others'


def _type_condition(type_key):
    """SQL ('cond', [params]) selecting items that fall in one type
    bucket, respecting the ITEM_TYPES priority order."""
    parts, params = [], []
    for key, _, kw in ITEM_TYPES:
        if key == type_key:
            parts.append("item_name LIKE ?")
            params.append(f"%{kw}%")
            break
        parts.append("item_name NOT LIKE ?")
        params.append(f"%{kw}%")
    return " AND ".join(parts), params


def _item_type_counts(db, where_sql='', where_params=()):
    """One aggregate query -> counts per type bucket (+ 'all'), for the
    filter-tab badges. Reflects the search/unit/rate filters currently
    applied but NOT the type filter itself, so every tab's count stays
    accurate and clickable no matter which tab is active."""
    select_parts, agg_params = [], []
    for key in TYPE_KEYS:
        cond, params = _type_condition(key)
        select_parts.append(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {key}")
        agg_params.extend(params)
    sql = f"SELECT {', '.join(select_parts)}, COUNT(*) AS all_count FROM items{where_sql}"
    row = db.execute(sql, agg_params + list(where_params)).fetchone()
    counts = {k: (row[k] or 0) for k in TYPE_KEYS}
    counts['all'] = row['all_count'] or 0
    return counts


class ItemPagesMixin:
    ITEMS_PAGE_SIZE = 10
    ITEMS_PAGE_SIZES = (10, 20, 50)
    ITEMS_SORT_COLUMNS = {
        'name': 'item_name',
        'description': 'description',
        'specification': 'specification',
        'unit': 'unit',
        'rate': 'rate_per_unit',
    }

    def handle_item_add(self, form):
        try:
            rate = float(form.get('rate_per_unit', 0))
        except (TypeError, ValueError):
            rate = 0
        db = get_db()
        description = form.get('description', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        specification = form.get('specification', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db.execute(
            "INSERT INTO items "
            "(item_name,description,specification,unit,rate_per_unit) "
            "VALUES (?,?,?,?,?)",
            (form.get('item_name', ''), description,
             specification, form.get('unit', 'Mtr.'), rate))
        db.commit()
        db.close()
        return self.send(*redirect('/items'))


    def handle_item_add_json(self, form):
        """AJAX twin of handle_item_add: used by the inline "+ New Item"
        modal on the quotation/proforma item picker so an item can be
        created (optionally tagged with a category) without losing
        whatever the user was already filling in. Returns the new row,
        including its category if one was set, as JSON instead of
        redirecting."""
        item_name = form.get('item_name', '').strip()
        if not item_name:
            return self.send(*json_response({'error': 'Item name is required.'}, 400))
        try:
            rate = float(form.get('rate_per_unit', 0))
        except (TypeError, ValueError):
            rate = None
        if rate is None or rate < 0:
            return self.send(*json_response({'error': 'Enter a valid rate.'}, 400))
        description = form.get('description', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        specification = form.get('specification', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        unit = form.get('unit', '').strip() or 'Mtr.'

        db = get_db()
        cur = db.execute(
            "INSERT INTO items (item_name,description,specification,unit,rate_per_unit) "
            "VALUES (?,?,?,?,?)",
            (item_name, description, specification, unit, rate))
        new_id = cur.lastrowid

        # Optional category, same as the "Add Category" flow: either an
        # existing category id, or a brand-new name typed on the spot
        # (created if it doesn't already exist). At most one either way.
        cat_row = None
        new_category_name = form.get('new_category_name', '').strip()
        category_id = form.get('category_id', '').strip()
        if new_category_name:
            db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (new_category_name,))
            cat_row = db.execute(
                "SELECT * FROM categories WHERE name=?", (new_category_name,)).fetchone()
        elif category_id:
            try:
                cat_row = db.execute(
                    "SELECT * FROM categories WHERE id=?", (int(category_id),)).fetchone()
            except (TypeError, ValueError):
                cat_row = None
        categories = []
        if cat_row:
            db.execute(
                "INSERT OR IGNORE INTO item_categories (category_id, item_id) VALUES (?,?)",
                (cat_row['id'], new_id))
            categories.append({'id': cat_row['id'], 'name': cat_row['name']})

        db.commit()
        db.close()
        return self.send(*json_response({
            'id': new_id,
            'name': item_name,
            'desc': description,
            'spec': specification,
            'unit': unit,
            'rate': rate,
            'categories': categories,
        }))


    def handle_item_edit(self, iid, form):
        try:
            rate = float(form.get('rate_per_unit', 0))
        except (TypeError, ValueError):
            rate = 0
        db = get_db()
        description = form.get('description', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        specification = form.get('specification', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        db.execute(
            "UPDATE items SET item_name=?,description=?,specification=?,"
            "unit=?,rate_per_unit=? WHERE id=?",
            (form.get('item_name', ''), description,
             specification, form.get('unit', 'Mtr.'),
             rate, iid))
        db.commit()
        db.close()
        return self.send(*redirect('/items'))


    # ── PAGE: ITEMS ──────────────────────────────────────────
    def _items_rows_and_modals_html(self, rows_data, start_num=1):
        rows = ''
        modals = ''
        for offset, i in enumerate(rows_data):
            row_num = start_num + offset
            spec_lines = [ln.strip() for ln in (i["specification"] or '').split('\n') if ln.strip()]
            if not spec_lines:
                spec_html = '<span class="text-muted">—</span>'
            else:
                first_two = ''.join(f'<li>{esc(ln)}</li>' for ln in spec_lines[:2])
                rest = spec_lines[2:]
                extra = ''.join(f'<li class="spec-extra d-none">{esc(ln)}</li>' for ln in rest)
                toggle = ''
                if rest:
                    toggle = (f'<button type="button" class="spec-toggle" onclick="toggleSpec(this)">'
                              f'<span class="more-label">+{len(rest)} more</span>'
                              f'<span class="less-label d-none">Show less</span>'
                              f'<i class="bi bi-chevron-down"></i></button>')
                spec_html = f'<ul class="spec-list">{first_two}{extra}</ul>{toggle}'

            name_key = esc((i["item_name"] or '').lower())
            desc_key = esc((i["description"] or '').lower())
            rows += (f'<tr data-name="{name_key}" data-desc="{desc_key}" data-rate="{i["rate_per_unit"]}">'
                     f'<td class="text-center"><input type="checkbox" class="item-row-check form-check-input" value="{i["id"]}" aria-label="Select {esc(i["item_name"])}"></td>'
                     f'<td class="text-muted item-row-num">{row_num}</td>'
                     f'<td><strong>{esc(i["item_name"])}</strong></td>'
                     f'<td class="text-muted" style="white-space:pre-line">{render_formatted(i["description"]) if i["description"] else "-"}</td>'
                     f'<td>{spec_html}</td>'
                     f'<td>{esc(i["unit"])}</td>'
                     f'<td class="text-end"><strong class="item-rate">Rs. {fmt_rupee(i["rate_per_unit"])}</strong>'
                     f'<div class="item-rate-unit">/ {esc(i["unit"])}</div></td>'
                     f'<td class="text-end">'
                     f'<button class="table-icon-btn" data-bs-toggle="modal" data-bs-target="#edit{i["id"]}" aria-label="Edit {esc(i["item_name"])}"><i class="bi bi-pencil"></i></button>'
                     f'<button class="table-icon-btn table-icon-btn-danger" data-bs-toggle="modal" data-bs-target="#del{i["id"]}" aria-label="Delete {esc(i["item_name"])}"><i class="bi bi-trash"></i></button>'
                     f'</td></tr>')
            modals += f'''
<div class="modal fade" id="edit{i["id"]}" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/items/{i["id"]}/edit">
      <div class="modal-header"><h5 class="modal-title">Edit Item</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="mb-2"><label class="form-label">Item Name</label>
          <input name="item_name" class="form-control" value="{esc(i["item_name"])}" required></div>
        <div class="mb-2 desc-field-wrap">
          <div class="d-flex justify-content-between align-items-center">
            <label class="form-label mb-1">Description</label>
            <button type="button" class="btn btn-sm btn-outline-secondary desc-bold-btn" onclick="toggleBoldSelection(this)" title="Bold the selected text">
              <i class="bi bi-type-bold"></i> Bold
            </button>
          </div>
          <textarea name="description" id="desc-edit-{i["id"]}" class="form-control" rows="3">{esc(i["description"] or "")}</textarea>
          <div class="form-text">Select text, then click Bold (or wrap it yourself in **like this**) to show it in bold.</div>
        </div>
        <div class="mb-2"><label class="form-label">Specification</label>
          <textarea name="specification" class="form-control" rows="4">{esc(i["specification"] or "")}</textarea></div>
        <div class="row">
          <div class="col-6 mb-2"><label class="form-label">Unit</label>
            <input name="unit" class="form-control" value="{esc(i["unit"])}"></div>
          <div class="col-6 mb-2"><label class="form-label">Rate (Rs.)</label>
            <input name="rate_per_unit" type="number" class="form-control" value="{i["rate_per_unit"]}" step="0.01"></div>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-lg"></i> Save</button>
      </div>
    </form>
  </div></div>
</div>'''
            desc_suffix = f' ({render_formatted(i["description"])})' if i["description"] else ''
            modals += f'''
<div class="modal fade" id="del{i["id"]}" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content delete-modal-content">
    <div class="modal-body">
      <h5 class="delete-modal-title">Delete item?</h5>
      <p class="delete-modal-text">This removes <strong>{esc(i["item_name"])}</strong>{desc_suffix} from the catalog. This can't be undone.</p>
    </div>
    <div class="modal-footer delete-modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <form method="POST" action="/items/{i["id"]}/delete" class="d-inline">
        <button type="submit" class="btn btn-danger">Delete</button>
      </form>
    </div>
  </div></div>
</div>'''
        return rows, modals

    def _items_empty_state(self, has_any, q):
        """Illustration + message for zero results, whether the catalog is
        truly empty or just has no matches for the current filters."""
        if not has_any:
            return ('<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-box-seam"></i></div>'
                    '<h6>No items yet</h6>'
                    '<p class="text-muted">Once you add an item, it will show up here.</p>'
                    '<button type="button" class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addItem">'
                    '<i class="bi bi-plus-circle"></i> Add your first item</button>'
                    '</div>')
        return ('<div class="q-empty">'
                '<div class="q-empty-icon"><i class="bi bi-search"></i></div>'
                '<h6>No matching items</h6>'
                f'<p class="text-muted">No items match the current filters{" (\u201c" + esc(q) + "\u201d)" if q else ""}.</p>'
                '<button type="button" class="btn btn-outline-secondary btn-sm" onclick="itClearFilters()">'
                '<i class="bi bi-x-circle"></i> Clear filters</button>'
                '</div>')

    def _items_pagination_html(self, page, total_pages):
        if total_pages <= 1:
            return ''
        html = '<nav aria-label="Items pagination"><ul class="pagination pagination-sm q-pagination mb-0">'

        def page_item(p, label, disabled=False, active=False):
            cls = 'page-item'
            if disabled:
                cls += ' disabled'
            if active:
                cls += ' active'
            return (f'<li class="{cls}"><a class="page-link" href="#" '
                    f'data-page="{p}" onclick="itGoToPage(event,{p})">{label}</a></li>')

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

    def _items_fragment(self, q='', page='1', sort='name', direction='asc',
                         type_filter='', units='', rate_min='', rate_max='',
                         page_size='10'):
        """Fetch, filter, sort, and paginate items; return rendered HTML
        fragments used both for the initial page load and the JSON API
        that powers live search/sort/filter/pagination without a full
        page reload."""
        q = (q or '').strip()
        type_filter = (type_filter or '').strip()
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = self.ITEMS_PAGE_SIZE
        if page_size not in self.ITEMS_PAGE_SIZES:
            page_size = self.ITEMS_PAGE_SIZE

        sort_col = self.ITEMS_SORT_COLUMNS.get(sort, 'item_name')
        direction_sql = 'DESC' if str(direction).lower() == 'desc' else 'ASC'

        unit_list = [u.strip() for u in (units or '').split(',') if u.strip()]
        try:
            rate_min_v = float(rate_min) if rate_min not in (None, '') else None
        except (TypeError, ValueError):
            rate_min_v = None
        try:
            rate_max_v = float(rate_max) if rate_max not in (None, '') else None
        except (TypeError, ValueError):
            rate_max_v = None

        db = get_db()

        # "base" = search + unit + rate filters only (no type filter) —
        # this is what the tab counts are computed against, so every
        # tab's number stays correct regardless of which tab is active.
        base_where, base_params = [], []
        if q:
            like = f"%{q}%"
            base_where.append("(item_name LIKE ? OR description LIKE ?)")
            base_params.extend([like, like])
        if unit_list:
            placeholders = ','.join('?' * len(unit_list))
            base_where.append(f"unit IN ({placeholders})")
            base_params.extend(unit_list)
        if rate_min_v is not None:
            base_where.append("rate_per_unit >= ?")
            base_params.append(rate_min_v)
        if rate_max_v is not None:
            base_where.append("rate_per_unit <= ?")
            base_params.append(rate_max_v)
        base_where_sql = (" WHERE " + " AND ".join(base_where)) if base_where else ""

        type_counts = _item_type_counts(db, base_where_sql, base_params)

        full_where = list(base_where)
        full_params = list(base_params)
        if type_filter and type_filter in TYPE_KEYS:
            cond, params = _type_condition(type_filter)
            full_where.append(f"({cond})")
            full_params.extend(params)
        full_where_sql = (" WHERE " + " AND ".join(full_where)) if full_where else ""

        has_any = db.execute("SELECT COUNT(*) c FROM items").fetchone()['c'] > 0
        total = db.execute(f"SELECT COUNT(*) c FROM items{full_where_sql}", full_params).fetchone()['c']
        total_pages = max(1, math.ceil(total / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        rows_data = db.execute(
            f"SELECT * FROM items{full_where_sql} ORDER BY {sort_col} {direction_sql} LIMIT ? OFFSET ?",
            full_params + [page_size, offset]).fetchall()

        all_units = [r['unit'] for r in db.execute(
            "SELECT DISTINCT unit FROM items WHERE unit IS NOT NULL AND unit != '' ORDER BY unit").fetchall()]
        db.close()

        if rows_data:
            rows_html, modals_html = self._items_rows_and_modals_html(rows_data, start_num=offset + 1)
        else:
            rows_html = f'<tr><td colspan="8" class="p-0">{self._items_empty_state(has_any, q)}</td></tr>'
            modals_html = ''

        range_start = 0 if total == 0 else offset + 1
        range_end = min(offset + page_size, total)
        count_label = (f'Showing {range_start}&ndash;{range_end} of {total}'
                       if total else 'No items found')

        return {
            'rows_html': rows_html,
            'modals_html': modals_html,
            'pagination_html': self._items_pagination_html(page, total_pages),
            'count_label': count_label,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'type_counts': type_counts,
            'all_units': all_units,
        }

    # ── PAGE: ITEMS ──────────────────────────────────────────
    def pg_items(self, q='', page='1', sort='name', direction='asc', type_filter=''):
        frag = self._items_fragment(q=q, page=page, sort=sort, direction=direction, type_filter=type_filter)

        db = get_db()
        total_items_overall = db.execute("SELECT COUNT(*) c FROM items").fetchone()['c']
        db.close()

        def _tab(key, label):
            active = ' active' if (type_filter or '') == key else ''
            count = frag['type_counts']['all'] if key == '' else frag['type_counts'].get(key, 0)
            return (f'<button type="button" class="type-tab{active}" data-type="{key}" onclick="itSetType(\'{key}\')">'
                    f'{esc(label)} <span class="type-tab-count">{count}</span></button>')

        type_tabs_html = _tab('', 'All Items') + ''.join(_tab(k, TYPE_LABELS[k]) for k in TYPE_KEYS)

        unit_checkboxes_html = ''.join(
            f'<label class="filters-unit-item"><input type="checkbox" class="filter-unit-check" value="{esc(u)}"> {esc(u)}</label>'
            for u in frag['all_units']
        ) or '<div class="text-muted small">No units yet</div>'

        page_size_options = ''.join(
            f'<option value="{sz}"{" selected" if sz == frag["page_size"] else ""}>{sz} / page</option>'
            for sz in self.ITEMS_PAGE_SIZES)

        common_units = ['Mtr.', 'Pcs.', 'Kg.', 'Set', 'Box']
        extra_units = [u for u in frag['all_units'] if u not in common_units]
        unit_datalist_options = ''.join(
            f'<option value="{esc(u)}">' for u in common_units + extra_units)

        content = f'''
<style>
.catalog-header {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px; margin-bottom:20px; }}
.catalog-icon-box {{ width:52px; height:52px; border-radius:14px; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:22px; flex-shrink:0; }}
.catalog-title {{ font-size:20px; font-weight:700; color:#0F172A; margin:0; }}
.catalog-subtitle {{ font-size:13px; color:#64748B; margin-top:2px; }}
.catalog-stat-chip {{ display:flex; align-items:center; gap:12px; background:#fff; border:1px solid #E2E8F0; border-radius:var(--radius); padding:10px 18px; }}
.catalog-stat-icon {{ width:38px; height:38px; border-radius:10px; background:#ECFDF5; color:#10B981; display:flex; align-items:center; justify-content:center; font-size:17px; flex-shrink:0; }}
.catalog-stat-label {{ font-size:11px; color:#64748B; }}
.catalog-stat-value {{ font-size:20px; font-weight:700; color:#0F172A; line-height:1.1; }}

.catalog-toolbar-row {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:16px; }}
.type-tabs {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
.type-tab {{
  border:1px solid #E2E8F0; background:#fff; color:#475569; border-radius:999px;
  padding:7px 14px; font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px;
  transition:all .15s;
}}
.type-tab:hover {{ border-color:var(--primary); color:var(--primary); }}
.type-tab.active {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.type-tab-count {{ font-size:11px; background:rgba(0,0,0,0.08); border-radius:999px; padding:1px 7px; }}
.type-tab.active .type-tab-count {{ background:rgba(255,255,255,0.25); }}

.catalog-search {{
  display:flex; align-items:center; gap:8px; background:#fff; border:1px solid #CBD5E1;
  border-radius:var(--radius-sm); padding:7px 12px; width:220px;
}}
.catalog-search i {{ color:#94A3B8; font-size:13px; }}
.catalog-search input {{ border:none; outline:none; font-size:13px; flex:1; min-width:0; }}
.catalog-search-clear {{ border:none; background:transparent; color:#94A3B8; padding:0; line-height:1; }}
.catalog-search-clear:hover {{ color:var(--primary); }}

.filters-dropdown-wrap {{ position:relative; }}
.filters-dropdown-wrap .btn.active {{ background:var(--primary-bg); border-color:var(--primary); color:var(--primary); }}
.filters-panel {{
  position:absolute; top:calc(100% + 8px); right:0; width:260px; z-index:60;
  background:#fff; border:1px solid #E2E8F0; border-radius:var(--radius); box-shadow:var(--shadow-lg);
  padding:16px; display:flex; flex-direction:column; gap:14px;
}}
.filters-panel-label {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; color:#94A3B8; display:block; margin-bottom:8px; }}
.filters-unit-list {{ display:flex; flex-direction:column; gap:6px; max-height:140px; overflow-y:auto; }}
.filters-unit-item {{ font-size:13px; color:#334155; display:flex; align-items:center; gap:8px; }}
.filters-panel-actions {{ display:flex; justify-content:flex-end; gap:8px; }}

.bulk-toolbar {{
  display:flex; align-items:center; justify-content:space-between; gap:10px;
  background:var(--primary-bg); border-bottom:1px solid #FECACA; padding:10px 20px; font-size:13px; color:#7F1D1D;
}}

.items-table thead th {{ font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:#64748B; white-space:nowrap; }}
.sortable-th {{ cursor:pointer; user-select:none; }}
.sortable-th:hover {{ color:var(--primary); }}
.sort-icon {{ font-size:11px; margin-left:2px; color:#CBD5E1; }}
.sort-icon.active {{ color:var(--primary); }}
.item-row-num {{ font-size:12px; }}
.item-rate {{ display:block; }}
.item-rate-unit {{ font-size:11px; color:#94A3B8; }}

.spec-list {{ list-style:disc; margin:0; padding-left:16px; font-size:12.5px; color:#475569; }}
.spec-toggle {{ border:none; background:transparent; color:var(--primary); font-size:11px; font-weight:600; padding:2px 0 0; display:flex; align-items:center; gap:3px; }}
.spec-toggle .bi {{ transition:transform .15s; font-size:10px; }}
.spec-toggle.expanded .bi {{ transform:rotate(180deg); }}

.table-icon-btn {{
  width:30px; height:30px; border-radius:var(--radius-sm); border:1px solid #E2E8F0;
  background:#fff; color:#475569; display:inline-flex; align-items:center; justify-content:center;
  transition:all .15s; margin-left:4px;
}}
.table-icon-btn:hover {{ background:#F8FAFC; border-color:#CBD5E1; }}
.table-icon-btn-danger {{ color:var(--primary); border-color:#FECACA; }}
.table-icon-btn-danger:hover {{ background:var(--primary-bg); border-color:var(--primary); }}

.q-empty {{ text-align:center; padding:56px 20px; }}
.q-empty-icon {{ width:64px; height:64px; border-radius:50%; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:26px; margin:0 auto 16px; }}
.q-empty h6 {{ margin-bottom:6px; font-weight:600; color:#0F172A; }}
.q-count-label {{ font-size:13px; color:#64748B; }}
#itemsBody {{ transition:opacity .1s; }}
#itemsBody.q-loading {{ opacity:.4; }}
.q-pagination .page-link {{ color:var(--primary); }}
.q-pagination .page-item.active .page-link {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
.q-pagination .page-item.disabled .page-link {{ color:#94A3B8; }}
.desc-field-wrap .desc-bold-btn {{ padding:2px 8px; font-size:12px; line-height:1.4; }}
.desc-field-wrap .form-text {{ font-size:12px; }}

@media (max-width: 767px) {{
  .catalog-header {{ align-items:flex-start; }}
  .catalog-toolbar-row {{ flex-direction:column; align-items:stretch; }}
  .catalog-search {{ width:100%; }}
  .type-tabs {{ overflow-x:auto; flex-wrap:nowrap; padding-bottom:4px; }}
}}
</style>

<div class="catalog-header">
  <div class="d-flex align-items-center gap-3">
    <div class="catalog-icon-box"><i class="bi bi-box-seam"></i></div>
    <div>
      <h4 class="catalog-title">Item Catalog</h4>
      <div class="catalog-subtitle">Manage your items, specifications and pricing</div>
    </div>
  </div>
  <div class="d-flex align-items-center gap-3 flex-wrap">
    <div class="catalog-stat-chip">
      <div class="catalog-stat-icon"><i class="bi bi-grid-3x3-gap-fill"></i></div>
      <div>
        <div class="catalog-stat-label">Total Items</div>
        <div class="catalog-stat-value">{total_items_overall}</div>
      </div>
    </div>
    <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addItem">
      <i class="bi bi-plus-circle"></i> Add New Item</button>
  </div>
</div>

<div class="catalog-toolbar-row">
  <div class="type-tabs" id="typeTabs">{type_tabs_html}</div>
  <div class="d-flex align-items-center gap-2 flex-wrap">
    <div class="catalog-search">
      <i class="bi bi-search"></i>
      <input type="text" id="itemSearch" placeholder="Search items..." autocomplete="off" value="{esc(q)}">
      <button type="button" id="itemSearchClear" class="catalog-search-clear d-none" onclick="itClearFilters()" aria-label="Clear search"><i class="bi bi-x"></i></button>
    </div>
    <div class="filters-dropdown-wrap">
      <button type="button" class="btn btn-outline-secondary btn-sm" id="filtersBtn"><i class="bi bi-funnel"></i> Filters <i class="bi bi-chevron-down"></i></button>
      <div class="filters-panel" id="filtersPanel" hidden>
        <div>
          <span class="filters-panel-label">Unit</span>
          <div class="filters-unit-list">{unit_checkboxes_html}</div>
        </div>
        <div>
          <span class="filters-panel-label">Rate range (Rs.)</span>
          <div class="d-flex gap-2">
            <input type="number" id="rateMin" class="form-control form-control-sm" placeholder="Min">
            <input type="number" id="rateMax" class="form-control form-control-sm" placeholder="Max">
          </div>
        </div>
        <div class="filters-panel-actions">
          <button type="button" class="btn btn-sm btn-outline-secondary" id="filtersClear">Clear</button>
          <button type="button" class="btn btn-sm btn-primary" id="filtersApply">Apply</button>
        </div>
      </div>
    </div>
    <a href="/categories" class="btn btn-outline-secondary btn-sm"><i class="bi bi-tags"></i> Categories</a>
  </div>
</div>

<div class="table-card">
  <div class="bulk-toolbar d-none" id="bulkToolbar">
    <span><strong id="bulkCount">0</strong> selected</span>
    <div class="d-flex gap-2">
      <button type="button" class="btn btn-sm btn-outline-secondary" onclick="itClearSelection()">Clear</button>
      <button type="button" class="btn btn-sm btn-outline-danger" data-bs-toggle="modal" data-bs-target="#bulkDeleteModal"><i class="bi bi-trash"></i> Delete Selected</button>
    </div>
  </div>
  <div class="table-responsive-wrapper">
  <table class="table mb-0 items-table">
    <thead>
      <tr>
        <th style="width:36px"><input type="checkbox" id="itemSelectAll" aria-label="Select all items"></th>
        <th style="width:36px">#</th>
        <th class="sortable-th" onclick="itSortBy('name')">Item Name <i class="bi bi-chevron-expand sort-icon" id="sortIcon-name"></i></th>
        <th class="sortable-th" onclick="itSortBy('description')">Description <i class="bi bi-chevron-expand sort-icon" id="sortIcon-description"></i></th>
        <th class="sortable-th" onclick="itSortBy('specification')">Specification <i class="bi bi-chevron-expand sort-icon" id="sortIcon-specification"></i></th>
        <th class="sortable-th" onclick="itSortBy('unit')">Unit <i class="bi bi-chevron-expand sort-icon" id="sortIcon-unit"></i></th>
        <th class="text-end sortable-th" onclick="itSortBy('rate')">Rate (Rs.) <i class="bi bi-chevron-expand sort-icon" id="sortIcon-rate"></i></th>
        <th class="text-end">Actions</th>
      </tr>
    </thead>
    <tbody id="itemsBody">{frag["rows_html"]}</tbody>
  </table>
  </div>
  <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between px-3 py-2 border-top">
    <span class="q-count-label" id="itCountLabel">{frag["count_label"]}</span>
    <div class="d-flex align-items-center gap-2">
      <div id="itPagination">{frag["pagination_html"]}</div>
      <select id="pageSizeSelect" class="form-select form-select-sm" style="width:auto">{page_size_options}</select>
    </div>
  </div>
</div>

<div id="itemsModals">{frag["modals_html"]}</div>

<div class="modal fade" id="bulkDeleteModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content delete-modal-content">
    <div class="modal-body">
      <h5 class="delete-modal-title">Delete <span id="bulkDeleteCount">0</span> items?</h5>
      <p class="delete-modal-text">This removes the selected items from the catalog. This can't be undone.</p>
    </div>
    <div class="modal-footer delete-modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <form method="POST" action="/items/bulk-delete" class="d-inline">
        <input type="hidden" name="ids" id="bulkDeleteIds" value="">
        <button type="submit" class="btn btn-danger">Delete</button>
      </form>
    </div>
  </div></div>
</div>

<div class="modal fade" id="addItem" tabindex="-1">
  <div class="modal-dialog modal-lg"><div class="modal-content">
    <form method="POST" action="/items/add">
      <div class="modal-icon-header">
        <div class="mih-left">
          <div class="mih-icon"><i class="bi bi-box-seam"></i></div>
          <div>
            <h5 class="mih-title">Add Item</h5>
            <div class="mih-subtitle">Add a new item to your catalog. This item will be available in quotations and invoices.</div>
          </div>
        </div>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <div class="mb-3">
          <label class="form-label">Item Name<span class="required-mark">*</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-box field-icon"></i>
            <input name="item_name" class="form-control" required placeholder="Enter item name">
          </div>
          <div class="field-hint">e.g. Sugar Elevator Chain 66.60 MM Pitch</div>
        </div>

        <div class="mb-3 desc-field-wrap">
          <div class="field-label-row">
            <label class="form-label">Description<span class="required-mark">*</span></label>
            <div class="field-actions">
              <button type="button" class="btn btn-outline-secondary desc-bold-btn" onclick="toggleBoldSelection(this)" title="Bold the selected text">B</button>
              <button type="button" class="btn btn-outline-secondary desc-preview-btn" onclick="toggleDescPreview(this)" title="Preview formatted text"><i class="bi bi-eye"></i> Preview</button>
            </div>
          </div>
          <div class="field-icon-wrap">
            <i class="bi bi-file-earmark-text field-icon"></i>
            <textarea name="description" id="desc-add" class="form-control" rows="3" maxlength="500" required
              placeholder="Enter item description" oninput="updateCounter(this,'desc-add-counter')"></textarea>
          </div>
          <div class="field-preview-box d-none" id="desc-add-preview"></div>
          <div class="field-hint-row">
            <div class="field-hint">You can select text and click <strong>Bold</strong> (or use **text**) to make it bold.</div>
            <div class="field-counter" id="desc-add-counter">0/500</div>
          </div>
        </div>

        <div class="mb-3">
          <label class="form-label">Specification <span class="text-muted fw-normal">(Optional)</span></label>
          <div class="field-icon-wrap">
            <i class="bi bi-gear field-icon"></i>
            <textarea name="specification" class="form-control" rows="4" maxlength="500"
              placeholder="Enter technical specification, material, size, etc." oninput="updateCounter(this,'spec-add-counter')"></textarea>
          </div>
          <div class="field-hint-row">
            <div class="field-hint">e.g. Material: SS304, Pitch: 66.60 MM, Breaking Load: 120 kN</div>
            <div class="field-counter" id="spec-add-counter">0/500</div>
          </div>
        </div>

        <div class="row">
          <div class="col-md-6 mb-3">
            <label class="form-label">Unit<span class="required-mark">*</span></label>
            <div class="field-icon-wrap">
              <i class="bi bi-tag field-icon"></i>
              <input name="unit" class="form-control" list="unitOptions" value="Mtr." autocomplete="off" required>
            </div>
            <datalist id="unitOptions">{unit_datalist_options}</datalist>
            <div class="field-hint">e.g. Mtr., Pcs., Kg., Set, Box</div>
          </div>
          <div class="col-md-6 mb-3">
            <label class="form-label">Rate (Rs.)<span class="required-mark">*</span></label>
            <div class="field-icon-wrap">
              <i class="bi bi-currency-rupee field-icon"></i>
              <input name="rate_per_unit" type="number" class="form-control" step="0.01" min="0" placeholder="Enter rate" required>
            </div>
            <div class="field-hint">Enter the selling price per unit (in INR)</div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <div class="modal-footer-split">
          <div class="info-tip-box">
            <i class="bi bi-info-circle-fill"></i>
            <div class="info-tip-text"><strong>Tip:</strong> Keep item details clear and accurate. These will be used in quotations and invoices.</div>
          </div>
          <div class="footer-actions">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-plus-lg"></i> Add Item</button>
          </div>
        </div>
      </div>
    </form>
  </div></div>
</div>

<script>
function toggleBoldSelection(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var ta = wrap ? wrap.querySelector('textarea') : null;
  if (!ta) return;
  var start = ta.selectionStart, end = ta.selectionEnd;
  if (start === end) {{
    alert('Select the text you want to bold first.');
    return;
  }}
  var val = ta.value;
  var selected = val.slice(start, end);
  var before = val.slice(0, start);
  var after = val.slice(end);
  var alreadyBold = selected.length >= 4 && selected.startsWith('**') && selected.endsWith('**');
  var replacement = alreadyBold ? selected.slice(2, -2) : ('**' + selected + '**');
  ta.value = before + replacement + after;
  ta.focus();
  ta.selectionStart = start;
  ta.selectionEnd = start + replacement.length;
}}

function updateCounter(el, counterId) {{
  var counter = document.getElementById(counterId);
  if (!counter) return;
  var max = el.getAttribute('maxlength');
  var len = el.value.length;
  counter.textContent = len + '/' + max;
  counter.classList.toggle('near-limit', !!max && len > max * 0.85 && len < max);
  counter.classList.toggle('at-limit', !!max && len >= max);
}}

function formatPreviewText(text) {{
  var div = document.createElement('div');
  div.textContent = text || '';
  var escaped = div.innerHTML;
  var formatted = escaped.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>').replace(/\\n/g, '<br>');
  return formatted || '<span class="text-muted">Nothing to preview yet.</span>';
}}

function toggleDescPreview(btn) {{
  var wrap = btn.closest('.desc-field-wrap');
  var iconWrap = wrap.querySelector('.field-icon-wrap');
  var ta = iconWrap.querySelector('textarea');
  var previewBox = wrap.querySelector('.field-preview-box');
  var showing = btn.classList.toggle('active');
  if (showing) {{
    previewBox.innerHTML = formatPreviewText(ta.value);
    iconWrap.classList.add('d-none');
    previewBox.classList.remove('d-none');
    btn.innerHTML = '<i class="bi bi-eye-slash"></i> Edit';
  }} else {{
    iconWrap.classList.remove('d-none');
    previewBox.classList.add('d-none');
    btn.innerHTML = '<i class="bi bi-eye"></i> Preview';
  }}
}}

(function() {{
  var state = {{
    q: {json_for_script(q)}, page: {frag["page"]}, sort: {json_for_script(sort)}, dir: {json_for_script(direction)},
    type: {json_for_script(type_filter)}, units: [], rateMin: '', rateMax: '', pageSize: {frag["page_size"]}
  }};
  var debounceTimer = null;
  var selected = new Set();

  function updateSortIcons() {{
    ['name', 'description', 'specification', 'unit', 'rate'].forEach(function (k) {{
      var icon = document.getElementById('sortIcon-' + k);
      icon.className = 'bi sort-icon' + (k === state.sort ? '' : ' bi-chevron-expand');
      if (k === state.sort) {{
        icon.classList.add(state.dir === 'asc' ? 'bi-chevron-up' : 'bi-chevron-down');
        icon.classList.add('active');
      }} else {{
        icon.classList.remove('active');
      }}
    }});
  }}

  function updateTabCounts(counts) {{
    if (!counts) return;
    document.querySelectorAll('.type-tab').forEach(function (btn) {{
      var key = btn.dataset.type || 'all';
      var span = btn.querySelector('.type-tab-count');
      if (span && counts[key] !== undefined) span.textContent = counts[key];
    }});
  }}

  function updateBulkToolbar() {{
    var n = selected.size;
    document.getElementById('bulkToolbar').classList.toggle('d-none', n === 0);
    document.getElementById('bulkCount').textContent = n;
  }}

  function bindRowCheckboxes() {{
    document.querySelectorAll('.item-row-check').forEach(function (cb) {{
      cb.checked = selected.has(cb.value);
      cb.addEventListener('change', function () {{
        if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
        updateBulkToolbar();
        syncSelectAll();
      }});
    }});
    syncSelectAll();
  }}

  function syncSelectAll() {{
    var boxes = document.querySelectorAll('.item-row-check');
    var all = boxes.length > 0 && Array.from(boxes).every(function (cb) {{ return cb.checked; }});
    var selectAll = document.getElementById('itemSelectAll');
    if (selectAll) selectAll.checked = all;
  }}

  function refresh() {{
    selected.clear();
    updateBulkToolbar();
    var tbody = document.getElementById('itemsBody');
    tbody.classList.add('q-loading');
    var params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    params.set('page', state.page);
    params.set('sort', state.sort);
    params.set('direction', state.dir);
    if (state.type) params.set('type', state.type);
    if (state.units.length) params.set('units', state.units.join(','));
    if (state.rateMin) params.set('rate_min', state.rateMin);
    if (state.rateMax) params.set('rate_max', state.rateMax);
    params.set('page_size', state.pageSize);
    fetch('/api/items/search?' + params.toString())
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        tbody.innerHTML = data.rows_html;
        tbody.classList.remove('q-loading');
        document.getElementById('itemsModals').innerHTML = data.modals_html;
        document.getElementById('itPagination').innerHTML = data.pagination_html;
        document.getElementById('itCountLabel').innerHTML = data.count_label;
        document.getElementById('itemSearchClear').classList.toggle('d-none', !state.q);
        updateSortIcons();
        updateTabCounts(data.type_counts);
        bindRowCheckboxes();
        var url = new URL(window.location);
        if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
        if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
        if (state.type) url.searchParams.set('type', state.type); else url.searchParams.delete('type');
        window.history.replaceState({{}}, '', url);
      }})
      .catch(function () {{ tbody.classList.remove('q-loading'); }});
  }}

  window.itGoToPage = function (e, p) {{
    e.preventDefault();
    if (p < 1) return;
    state.page = p;
    refresh();
  }};

  window.itSortBy = function (key) {{
    state.dir = (state.sort === key && state.dir === 'asc') ? 'desc' : 'asc';
    state.sort = key;
    state.page = 1;
    refresh();
  }};

  window.itSetType = function (type) {{
    state.type = type;
    state.page = 1;
    document.querySelectorAll('.type-tab').forEach(function (btn) {{
      btn.classList.toggle('active', btn.dataset.type === type);
    }});
    refresh();
  }};

  window.itClearFilters = function () {{
    state.q = '';
    state.page = 1;
    document.getElementById('itemSearch').value = '';
    refresh();
  }};

  window.itClearSelection = function () {{
    selected.clear();
    document.querySelectorAll('.item-row-check').forEach(function (cb) {{ cb.checked = false; }});
    syncSelectAll();
    updateBulkToolbar();
  }};

  var searchInput = document.getElementById('itemSearch');
  searchInput.addEventListener('input', function (e) {{
    clearTimeout(debounceTimer);
    var val = e.target.value;
    debounceTimer = setTimeout(function () {{
      state.q = val;
      state.page = 1;
      refresh();
    }}, 350);
  }});
  searchInput.addEventListener('keydown', function (e) {{
    if (e.key === 'Enter') {{
      e.preventDefault();
      clearTimeout(debounceTimer);
      state.q = e.target.value;
      state.page = 1;
      refresh();
    }}
  }});

  document.getElementById('itemSelectAll').addEventListener('change', function (e) {{
    var checked = e.target.checked;
    document.querySelectorAll('.item-row-check').forEach(function (cb) {{
      cb.checked = checked;
      if (checked) selected.add(cb.value); else selected.delete(cb.value);
    }});
    updateBulkToolbar();
  }});

  document.getElementById('bulkDeleteModal').addEventListener('show.bs.modal', function () {{
    document.getElementById('bulkDeleteIds').value = Array.from(selected).join(',');
    document.getElementById('bulkDeleteCount').textContent = selected.size;
  }});

  var pageSizeSelect = document.getElementById('pageSizeSelect');
  pageSizeSelect.addEventListener('change', function (e) {{
    state.pageSize = parseInt(e.target.value, 10) || 10;
    state.page = 1;
    refresh();
  }});

  var filtersBtn = document.getElementById('filtersBtn');
  var filtersPanel = document.getElementById('filtersPanel');
  filtersBtn.addEventListener('click', function (e) {{
    e.stopPropagation();
    filtersPanel.hidden = !filtersPanel.hidden;
  }});
  document.addEventListener('click', function (e) {{
    if (!filtersPanel.hidden && !filtersPanel.contains(e.target) && !filtersBtn.contains(e.target)) {{
      filtersPanel.hidden = true;
    }}
  }});
  document.getElementById('filtersApply').addEventListener('click', function () {{
    state.units = Array.from(document.querySelectorAll('.filter-unit-check:checked')).map(function (cb) {{ return cb.value; }});
    state.rateMin = document.getElementById('rateMin').value;
    state.rateMax = document.getElementById('rateMax').value;
    state.page = 1;
    filtersBtn.classList.toggle('active', state.units.length > 0 || !!state.rateMin || !!state.rateMax);
    filtersPanel.hidden = true;
    refresh();
  }});
  document.getElementById('filtersClear').addEventListener('click', function () {{
    document.querySelectorAll('.filter-unit-check').forEach(function (cb) {{ cb.checked = false; }});
    document.getElementById('rateMin').value = '';
    document.getElementById('rateMax').value = '';
    state.units = []; state.rateMin = ''; state.rateMax = '';
    state.page = 1;
    filtersBtn.classList.remove('active');
    filtersPanel.hidden = true;
    refresh();
  }});

  updateSortIcons();
  bindRowCheckboxes();
}})();

function toggleSpec(btn) {{
  var cell = btn.closest('td');
  var extras = cell.querySelectorAll('.spec-extra');
  var expanded = btn.classList.toggle('expanded');
  extras.forEach(function (li) {{ li.classList.toggle('d-none', !expanded); }});
  cell.querySelector('.more-label').classList.toggle('d-none', expanded);
  cell.querySelector('.less-label').classList.toggle('d-none', !expanded);
}}
</script>'''
        return self._layout('Items', 'Item Catalog', content, 'items')
