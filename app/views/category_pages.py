from utils.http_utils import esc
from utils.http_utils import esc_nl
from utils.http_utils import fmt_rupee
from utils.http_utils import redirect
from database import get_db


class CategoryPagesMixin:

    def handle_category_add(self, form):
        db = get_db()
        name = form.get('name', '').strip()
        if name:
            db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
            db.commit()
        db.close()
        return self.send(*redirect('/categories'))

    def handle_category_edit(self, cid, form):
        db = get_db()
        name = form.get('name', '').strip()
        if name:
            db.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
            db.commit()
        db.close()
        return self.send(*redirect('/categories'))

    def handle_category_items(self, cid, form):
        db = get_db()
        # Clear existing assignments, then add whatever was checked —
        # same replace-all approach used for customer_items.
        db.execute("DELETE FROM item_categories WHERE category_id=?", (cid,))
        item_ids = form.getlist('item_ids[]')
        for iid in item_ids:
            try:
                iid = int(iid)
                db.execute("INSERT OR IGNORE INTO item_categories (category_id, item_id) VALUES (?,?)", (cid, iid))
            except (TypeError, ValueError):
                pass
        db.commit()
        db.close()
        return self.send(*redirect('/categories'))

    # ── PAGE: CATEGORIES ─────────────────────────────────────
    def pg_categories(self):
        db = get_db()
        cats = db.execute(
            "SELECT c.*, COUNT(ic.item_id) AS item_count "
            "FROM categories c LEFT JOIN item_categories ic ON ic.category_id = c.id "
            "GROUP BY c.id ORDER BY c.name").fetchall()
        db.close()

        rows = ''
        modals = ''
        for c in cats:
            item_count = c["item_count"]
            rows += (f'<tr><td><strong>{esc(c["name"])}</strong></td>'
                     f'<td>{item_count} item{"s" if item_count != 1 else ""}</td>'
                     f'<td class="text-end">'
                     f'<a href="/categories/{c["id"]}/items" class="btn btn-sm btn-outline-success me-1" title="Manage Items"><i class="bi bi-box-seam"></i></a>'
                     f'<button class="btn btn-sm btn-outline-primary me-1" data-bs-toggle="modal" data-bs-target="#editCat{c["id"]}" title="Rename"><i class="bi bi-pencil"></i></button>'
                     f'<form method="POST" action="/categories/{c["id"]}/delete" class="d-inline" onsubmit="return confirm(\'Delete this category? The items themselves will not be deleted.\')">'
                     f'<button class="btn btn-sm btn-outline-danger" title="Delete"><i class="bi bi-trash"></i></button></form></td></tr>')
            modals += f'''
<div class="modal fade" id="editCat{c["id"]}" tabindex="-1">
  <div class="modal-dialog"><div class="modal-content">
    <form method="POST" action="/categories/{c["id"]}/edit">
      <div class="modal-header"><h5 class="modal-title">Rename Category</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="mb-2"><label class="form-label">Category Name</label>
          <input name="name" class="form-control" value="{esc(c["name"])}" required></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-lg"></i> Save</button>
      </div>
    </form>
  </div></div>
</div>'''

        if not cats:
            rows = ('<tr><td colspan="3" class="p-0">'
                    '<div class="q-empty">'
                    '<div class="q-empty-icon"><i class="bi bi-tags"></i></div>'
                    '<h6>No categories yet</h6>'
                    '<p class="text-muted">Group items into a category &mdash; e.g. everything for one '
                    'particular sugar mill &mdash; so you can filter to just those items when building a quotation.</p>'
                    '<button type="button" class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addCat">'
                    '<i class="bi bi-plus-circle"></i> Add your first category</button>'
                    '</div></td></tr>')

        content = f'''
<style>
.q-empty {{ text-align:center; padding:56px 20px; }}
.q-empty-icon {{ width:64px; height:64px; border-radius:50%; background:var(--primary-bg); color:var(--primary); display:flex; align-items:center; justify-content:center; font-size:26px; margin:0 auto 16px; }}
.q-empty h6 {{ margin-bottom:6px; font-weight:600; color:#0F172A; }}
</style>
<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
  <h5 class="mb-0"><i class="bi bi-tags"></i> Item Categories ({len(cats)})</h5>
  <div class="d-flex align-items-center gap-2 flex-wrap">
    <a href="/items" class="btn btn-outline-secondary btn-sm"><i class="bi bi-arrow-left"></i> Back to Items</a>
    <button class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#addCat">
      <i class="bi bi-plus-circle"></i> Add Category</button>
  </div>
</div>
<div class="table-card">
  <table class="table mb-0">
    <thead><tr><th>Category</th><th>Items Assigned</th><th class="text-end">Actions</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{modals}
<div class="modal fade" id="addCat" tabindex="-1">
  <div class="modal-dialog"><div class="modal-content">
    <form method="POST" action="/categories/add">
      <div class="modal-header"><h5 class="modal-title">Add Category</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="mb-2"><label class="form-label">Category Name</label>
          <input name="name" class="form-control" placeholder="e.g. XYZ Sugar Mills" required></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-plus-lg"></i> Add</button>
      </div>
    </form>
  </div></div>
</div>'''
        return self._layout('Categories', 'Item Categories', content, 'items')

    # ── PAGE: CATEGORY ITEMS ─────────────────────────────────
    def pg_category_items(self, cid):
        db = get_db()
        category = db.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if not category:
            db.close()
            return self.pg_404()
        all_items = db.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        assigned = [r['item_id'] for r in db.execute(
            "SELECT item_id FROM item_categories WHERE category_id=?", (cid,)).fetchall()]
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
  <h5 class="mb-0"><i class="bi bi-box-seam"></i> Items in &quot;{esc(category["name"])}&quot;</h5>
  <a href="/categories" class="btn btn-outline-primary btn-sm"><i class="bi bi-arrow-left"></i> Back</a>
</div>
<form method="POST" action="/categories/{cid}/items">
<div class="table-card">
  <table class="table mb-0">
    <thead><tr><th style="width:50px">Select</th><th>Item Name</th><th>Description</th><th>Unit</th><th>Rate</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
<div class="mt-3">
  <button type="submit" class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-circle"></i> Save Items</button>
</div>
</form>'''
        return self._layout(f'Items - {category["name"]}', f'Items in {category["name"]}', content, 'items')
