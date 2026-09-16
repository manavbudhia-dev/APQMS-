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
        # decorative always-empty bell. The bell also opens a dropdown
        # listing the actual drafts so clicking it does something.
        draft_count = 0
        notif_rows = []
        try:
            db = get_db()
            draft_count = db.execute(
                "SELECT COUNT(*) c FROM quotations WHERE status='Draft'").fetchone()['c']
            notif_rows = db.execute(
                "SELECT q.id, q.quotation_no, q.date, c.company_name "
                "FROM quotations q LEFT JOIN customers c ON q.customer_id=c.id "
                "WHERE q.status='Draft' ORDER BY q.created_at DESC LIMIT 5").fetchall()
            db.close()
        except Exception:
            draft_count = 0
            notif_rows = []
        badge_html = ''
        if draft_count > 0:
            badge_text = '9+' if draft_count > 9 else str(draft_count)
            badge_html = f'<span class="notif-badge">{badge_text}</span>'
        bell_title = (f'{draft_count} draft quotation{"s" if draft_count != 1 else ""} need attention'
                      if draft_count > 0 else 'No pending drafts')

        if notif_rows:
            items_html = ''
            for r in notif_rows:
                company = esc(r['company_name'] or 'No customer')
                qno = esc(r['quotation_no'] or '')
                date = esc(r['date'] or '')
                items_html += (
                    f'<li><a class="dropdown-item notif-item" href="/quotations/{r["id"]}">'
                    f'<i class="bi bi-file-earmark-text"></i>'
                    f'<span class="notif-item-text">'
                    f'<span class="notif-item-title">{qno} &middot; {company}</span>'
                    f'<span class="notif-item-sub">Draft &middot; {date}</span>'
                    f'</span></a></li>')
        else:
            items_html = '<li><span class="dropdown-item-text notif-empty">No pending drafts</span></li>'
        notif_dropdown_html = f'''<div class="dropdown">
          <button type="button" class="topbar-icon-btn" data-bs-toggle="dropdown" aria-expanded="false" title="{esc(bell_title)}" aria-label="Notifications">
            <i class="bi bi-bell"></i>{badge_html}
          </button>
          <ul class="dropdown-menu dropdown-menu-end notif-dropdown">
            <li><h6 class="dropdown-header">Notifications</h6></li>
            {items_html}
            <li><hr class="dropdown-divider"></li>
            <li><a class="dropdown-item text-center small" href="/quotations?status=Draft">View all drafts</a></li>
          </ul>
        </div>'''

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
        {notif_dropdown_html}
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

// ── DATE PICKERS ─────────────────────────────────────────
// Every "Date" field in the app (quotations, proforma invoices,
// packing lists) is a plain text field storing "DD - MM - YYYY".
// The calendar icon next to it was decorative only. This wires a
// real native date picker to every input[name="date"]: clicking the
// field opens the OS/browser calendar, and the picked date is
// written back in the app's existing "DD - MM - YYYY" format so
// nothing downstream (storage, PDFs, listings) has to change.
(function () {{
  function pad(n) {{ return String(n).padStart(2, '0'); }}
  function toISO(text) {{
    if (!text) return '';
    var m = text.match(/(\\d{{1,2}})\\s*-\\s*(\\d{{1,2}})\\s*-\\s*(\\d{{4}})/);
    if (m) return m[3] + '-' + pad(m[2]) + '-' + pad(m[1]);
    var iso = text.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/);
    return iso ? text : '';
  }}
  function toDisplay(iso) {{
    var p = iso.split('-');
    return p.length === 3 ? (p[2] + ' - ' + p[1] + ' - ' + p[0]) : iso;
  }}
  function openPicker(hidden) {{
    if (typeof hidden.showPicker === 'function') {{
      try {{ hidden.showPicker(); return; }} catch (e) {{}}
    }}
    hidden.focus();
  }}
  function initDatePickers() {{
    document.querySelectorAll('input[name="date"]').forEach(function (txt) {{
      if (txt.dataset.pickerInit || txt.type === 'date') return;
      txt.dataset.pickerInit = '1';

      var wrap = txt.parentElement;
      wrap.classList.add('input-icon');
      wrap.style.position = 'relative';
      if (!wrap.querySelector('i.bi-calendar3, i.bi-calendar, i.bi-calendar-event')) {{
        var icon = document.createElement('i');
        icon.className = 'bi bi-calendar3';
        wrap.insertBefore(icon, txt);
      }}

      var hidden = document.createElement('input');
      hidden.type = 'date';
      hidden.tabIndex = -1;
      hidden.setAttribute('aria-hidden', 'true');
      hidden.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;' +
        'opacity:0;border:0;padding:0;margin:0;pointer-events:none;';
      wrap.appendChild(hidden);

      var iso = toISO(txt.value);
      if (iso) hidden.value = iso;

      txt.readOnly = true;
      txt.style.cursor = 'pointer';
      txt.addEventListener('click', function () {{ openPicker(hidden); }});

      hidden.addEventListener('change', function () {{
        if (hidden.value) {{
          txt.value = toDisplay(hidden.value);
          txt.dispatchEvent(new Event('input', {{ bubbles: true }}));
          txt.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
      }});
    }});
  }}
  document.addEventListener('DOMContentLoaded', initDatePickers);
}})();
</script>
</body>
</html>'''


