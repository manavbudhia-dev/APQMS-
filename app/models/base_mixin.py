from utils.http_utils import esc
from utils.http_utils import get_session
import os
from app.config import BASE
from database import get_db


class BaseMixin:
    def log_message(self, format, *args):
        pass  # suppress default request logging


    # ── LOW-LEVEL SEND ───────────────────────────────────────
    def send(self, body, status=200, headers=None):
        self.send_response(status)
        for h in (headers or []):
            self.send_header(h[0], h[1])
        self.end_headers()
        if body:
            self.wfile.write(body)


    def serve_static(self, path):
        fpath = os.path.join(BASE, path.lstrip('/'))
        if os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            ct = {
                '.css': 'text/css',
                '.js': 'application/javascript',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.svg': 'image/svg+xml',
                '.ico': 'image/x-icon',
                '.webp': 'image/webp',
                '.woff2': 'font/woff2',
            }.get(ext, 'text/plain')
            with open(fpath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not found')


    # ── LAYOUT ───────────────────────────────────────────────
    def _layout(self, title, heading, content, active):
        sess = get_session(self.headers.get('Cookie'))
        full_name = sess.get('full_name', 'User')
        role = sess.get('role', 'user')
        nav_items = [
            ('dashboard', '/dashboard', 'bi-speedometer2', 'Dashboard'),
            ('quotations', '/quotations', 'bi-file-earmark-text', 'Quotations'),
            ('proforma', '/proforma', 'bi-receipt', 'Proforma Invoices'),
            ('packing', '/packing', 'bi-box-seam', 'Packing Lists'),
            ('customers', '/customers', 'bi-people', 'Customers'),
            ('items', '/items', 'bi-box-seam', 'Items Catalog'),
            ('salary', '/salary', 'bi-cash-stack', 'Salary'),
        ]
        nav_html = ''
        for key, href, icon, label in nav_items:
            cls = ' class="active"' if key == active else ''
            nav_html += (f'<li><a href="{href}"{cls}>'
                         f'<i class="{icon}"></i><span>{label}</span></a></li>')

        # Draft count powers the header notification badge — a real,
        # cheap signal ("N drafts still need attention") rather than a
        # decorative always-empty bell.
        try:
            db = get_db()
            draft_count = db.execute(
                "SELECT COUNT(*) c FROM quotations WHERE status='Draft'").fetchone()['c']
            db.close()
        except Exception:
            draft_count = 0
        badge_html = ''
        if draft_count > 0:
            badge_text = '9+' if draft_count > 9 else str(draft_count)
            badge_html = f'<span class="notif-badge">{badge_text}</span>'
        bell_title = (f'{draft_count} draft quotation{"s" if draft_count != 1 else ""} need attention'
                      if draft_count > 0 else 'No pending drafts')

        initial = esc((full_name or 'U').strip()[:1].upper() or 'U')
        admin_link = ''
        if role == 'admin':
            admin_link = '<li><a class="dropdown-item" href="/register"><i class="bi bi-person-plus"></i> Add User</a></li>'

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Aypee Quotation System</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<link href="/static/css/style.css" rel="stylesheet">
</head>
<body>
<div class="d-flex">
  <nav class="sidebar" id="sidebar">
    <div class="sidebar-brand">
      <div class="brand-icon">A</div>
      <span class="brand-text">AYPEE</span>
    </div>
    <ul class="sidebar-nav">{nav_html}</ul>
    <div class="sidebar-footer">
      <div class="user-info"><i class="bi bi-person-circle"></i><span>{esc(full_name)}</span></div>
      <a href="/logout" class="btn-logout"><i class="bi bi-box-arrow-left"></i> Logout</a>
    </div>
  </nav>
  <div class="sidebar-overlay" onclick="document.getElementById('sidebar').classList.remove('show')"></div>
  <div class="main-content">
    <div class="topbar">
      <button class="btn btn-sm btn-sidebar-toggle" onclick="document.getElementById('sidebar').classList.toggle('show')"><i class="bi bi-list fs-4"></i></button>
      <h5 class="mb-0">{heading}</h5>
      <div class="topbar-actions">
        <label class="topbar-search d-none d-md-flex" for="globalSearch">
          <i class="bi bi-search"></i>
          <input type="text" id="globalSearch" placeholder="Search anything... (quotations, customers, items)" autocomplete="off">
          <kbd>Ctrl K</kbd>
        </label>
        <button type="button" class="topbar-icon-btn" title="{esc(bell_title)}" aria-label="Notifications">
          <i class="bi bi-bell"></i>{badge_html}
        </button>
        <div class="dropdown">
          <button class="avatar-chip" type="button" data-bs-toggle="dropdown" aria-expanded="false">
            <span class="avatar-circle">{initial}</span>
            <span class="avatar-meta d-none d-lg-flex">
              <strong>{esc(full_name)}</strong>
              <small>{esc(role.capitalize())}</small>
            </span>
            <i class="bi bi-chevron-down"></i>
          </button>
          <ul class="dropdown-menu dropdown-menu-end">
            <li><a class="dropdown-item" href="/change-password"><i class="bi bi-key"></i> Change Password</a></li>
            {admin_link}
            <li><hr class="dropdown-divider"></li>
            <li><a class="dropdown-item text-danger" href="/logout"><i class="bi bi-box-arrow-left"></i> Logout</a></li>
          </ul>
        </div>
      </div>
    </div>
    <div class="content-area">{content}</div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
document.addEventListener('keydown', function (e) {{
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {{
    e.preventDefault();
    var el = document.getElementById('globalSearch');
    if (el) el.focus();
  }}
}});
</script>
</body>
</html>'''


