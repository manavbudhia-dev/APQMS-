from utils.http_utils import esc
from utils.http_utils import get_session
from utils.http_utils import SECURITY_HEADERS
from utils.http_utils import NO_STORE_HEADERS
import os
from app.config import BASE
from database import get_db
from database import list_financial_years
from database import resolve_financial_year
from utils.dates import fy_start_year


class BaseMixin:
    def log_message(self, format, *args):
        pass  # suppress default request logging

    # ── FINANCIAL YEAR (current selection) ───────────────────
    def _current_fy(self):
        """The financial_years Row currently selected for this session, or
        None for "All Financial Years". Used by every list/dashboard page
        that scopes its data by document date; items and customers never
        call this — they're shared master data across all years."""
        sess = get_session(self.headers.get('Cookie'))
        db = get_db()
        try:
            fy = resolve_financial_year(db, sess.get('fy_id'))
            db.commit()  # resolve_financial_year may have created a row
            return fy
        finally:
            db.close()

    def _fy_switcher_html(self):
        """Topbar control (shown on every page, top-right) for switching
        between financial years and creating new ones. Quotations,
        proforma invoices and packing lists are scoped to whatever is
        selected here; items and customers are always shown in full."""
        db = get_db()
        try:
            sess = get_session(self.headers.get('Cookie'))
            fys = list_financial_years(db)
            current = resolve_financial_year(db, sess.get('fy_id'))
            db.commit()
        except Exception:
            fys, current = [], None
        finally:
            db.close()

        next_url = esc(getattr(self, 'path', '') or '/dashboard')
        current_label = f'FY {current["label"]}' if current else 'All Financial Years'

        def _option(value, label, icon, active):
            cls = ' fy-option-active' if active else ''
            check = '<i class="bi bi-check-lg fy-check"></i>' if active else ''
            return (f'<li><form method="POST" action="/financial-year/select">'
                    f'<input type="hidden" name="next" value="{next_url}">'
                    f'<input type="hidden" name="financial_year_id" value="{esc(value)}">'
                    f'<button type="submit" class="dropdown-item fy-option{cls}">'
                    f'<i class="bi {icon}"></i> {esc(label)}{check}</button>'
                    f'</form></li>')

        options_html = _option('all', 'All Financial Years', 'bi-infinity', current is None)
        for fy in fys:
            options_html += _option(fy['id'], f'FY {fy["label"]}', 'bi-calendar3',
                                     current is not None and fy['id'] == current['id'])

        suggested = (fys[0]['start_year'] + 1) if fys else fy_start_year()

        return f'''<div class="dropdown fy-switcher">
          <button type="button" class="fy-switcher-btn" data-bs-toggle="dropdown" aria-expanded="false" title="Switch financial year">
            <i class="bi bi-calendar-range"></i>
            <span class="d-none d-sm-inline">{esc(current_label)}</span>
            <i class="bi bi-chevron-down"></i>
          </button>
          <ul class="dropdown-menu dropdown-menu-end fy-switcher-menu">
            <li><h6 class="dropdown-header">Financial Year</h6></li>
            {options_html}
            <li><hr class="dropdown-divider"></li>
            <li>
              <form method="POST" action="/financial-year/add" class="fy-add-form">
                <input type="hidden" name="next" value="{next_url}">
                <label class="form-label d-block">Create Financial Year</label>
                <div class="input-group input-group-sm">
                  <span class="input-group-text">FY</span>
                  <input type="number" name="start_year" class="form-control" placeholder="{suggested}" min="2000" max="2099" required>
                  <button class="btn btn-primary" type="submit" title="Create"><i class="bi bi-plus-lg"></i></button>
                </div>
                <div class="form-text">e.g. {suggested} creates FY {suggested}-{str(suggested + 1)[-2:]} (1 Apr {suggested} &ndash; 31 Mar {suggested + 1})</div>
              </form>
            </li>
            <li class="fy-switcher-note">Items &amp; customers are shared across every financial year.</li>
          </ul>
        </div>'''


    # ── LOW-LEVEL SEND ───────────────────────────────────────
    def send(self, body, status=200, headers=None):
        self.send_response(status)
        for h in SECURITY_HEADERS:
            self.send_header(h[0], h[1])
        # Every response through this path is an authenticated app page,
        # a redirect, or a JSON/file API response — none of it should
        # linger in a shared or browser cache after logout. Static assets
        # (CSS/JS/images) go through serve_static() instead, which does
        # not set this, so they stay cacheable.
        for h in NO_STORE_HEADERS:
            self.send_header(h[0], h[1])
        for h in (headers or []):
            self.send_header(h[0], h[1])
        self.end_headers()
        if body:
            self.wfile.write(body)


    def serve_static(self, path):
        # Resolve symlinks/'..' and refuse anything that lands outside the
        # static/ folder — otherwise '/static/../quotation.db' would hand
        # out the database (or backups, source code) to anyone, logged in
        # or not.
        static_root = os.path.realpath(os.path.join(BASE, 'static'))
        fpath = os.path.realpath(os.path.join(BASE, path.lstrip('/')))
        if fpath.startswith(static_root + os.sep) and os.path.isfile(fpath):
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
            for h in SECURITY_HEADERS:
                self.send_header(h[0], h[1])
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            for h in SECURITY_HEADERS:
                self.send_header(h[0], h[1])
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not found')


    # ── LAYOUT ───────────────────────────────────────────────
    def _layout(self, title, heading, content, active, crumbs=None):
        # `title`/`heading` are frequently built from user-editable data
        # (a customer's company name, a category name, an employee name,
        # a quotation subject, ...) by the many pg_*() callers of this
        # method. Escaping once, centrally, here — rather than trusting
        # every call site to remember to wrap its own f-string in esc()
        # — is what actually closes that off: a company/category/
        # employee name containing HTML would otherwise render live in
        # the <title> and page heading of every page that displays it,
        # a stored-XSS hazard reachable by any ordinary logged-in user,
        # not just an admin. `content` is left as-is: it's built entirely
        # from this app's own already-escaped HTML fragments.
        title = esc(title)
        heading = esc(heading)
        # Optional breadcrumb trail [(label, href|None), ...] shown in
        # the top bar in place of the plain heading.
        if crumbs:
            parts = []
            for label, href in crumbs:
                if href:
                    parts.append(f'<a href="{esc(href)}">{esc(label)}</a>')
                else:
                    parts.append(f'<span class="crumb-current">{esc(label)}</span>')
            crumb_html = ('<nav class="topbar-crumbs" aria-label="Breadcrumb">'
                          + '<i class="bi bi-chevron-right"></i>'.join(parts) + '</nav>')
        else:
            crumb_html = f'<h5 class="mb-0">{heading}</h5>'
        sess = get_session(self.headers.get('Cookie'))
        full_name = sess.get('full_name', 'User')
        role = sess.get('role', 'user')
        nav_items = [
            ('dashboard', '/dashboard', 'bi-speedometer2', 'Dashboard'),
            ('quotations', '/quotations', 'bi-file-earmark-text', 'Quotations'),
            ('proforma', '/proforma', 'bi-receipt', 'Proforma Invoices'),
            ('packing', '/packing', 'bi-box-seam', 'Packing Lists'),
            ('ocr', '/ocr', 'bi-file-earmark-arrow-up', 'Scan & Import'),
            ('customers', '/customers', 'bi-people', 'Customers'),
            ('items', '/items', 'bi-box-seam', 'Items Catalog'),
            ('salary', '/salary', 'bi-cash-stack', 'Salary'),
            ('metrics', '/metrics', 'bi-graph-up-arrow', 'Business Metrics'),
            ('settings', '/settings', 'bi-gear', 'Settings'),
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
      {crumb_html}
      <div class="topbar-actions">
        {self._fy_switcher_html()}
        <label class="topbar-search d-none d-md-flex" for="globalSearch" style="position:relative">
          <i class="bi bi-search"></i>
          <input type="text" id="globalSearch" placeholder="Search anything... (quotations, customers, items)" autocomplete="off">
          <kbd>Ctrl K</kbd>
          <div id="globalSearchResults" class="global-search-results d-none"></div>
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
            <li><a class="dropdown-item" href="/settings"><i class="bi bi-gear"></i> Settings</a></li>
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

// ── APP-WIDE KEYBOARD SHORTCUTS ──────────────────────────────
// Ctrl/Cmd+S saves the page's form from anywhere, and a plain Enter
// in an ordinary field saves it too -- the same way a single-field
// form already submits on Enter natively, just made to work
// consistently everywhere (including forms that also contain other
// interactive controls, like the item-picker inside the proforma
// and quotation builders, where a bare Enter must NOT fire the
// whole save).
(function () {{
  function findSaveButton(form) {{
    return form.querySelector(
      'button[type="submit"]:not([disabled]), ' +
      'input[type="submit"]:not([disabled]), ' +
      'button:not([type]):not([disabled])'
    );
  }}

  function triggerSave(fromEl) {{
    var form = (fromEl && fromEl.closest) ? fromEl.closest('form') : null;
    if (!form) {{
      // Nothing focused inside a form (e.g. Ctrl+S pressed with the
      // page itself focused) -- fall back to the page's one obvious
      // form, if it has exactly one, rather than guessing.
      var forms = document.querySelectorAll('form');
      if (forms.length === 1) form = forms[0];
    }}
    if (!form) return false;
    var btn = findSaveButton(form);
    if (btn) {{
      btn.click();
    }} else if (typeof form.requestSubmit === 'function') {{
      form.requestSubmit();
    }} else {{
      form.submit();
    }}
    return true;
  }}

  // Fields that live inside a bigger "save" form but actually feed a
  // separate JS action (add this item to the list, add this new
  // customer) opt in via data-enter-action="someFunction()" so Enter
  // there does that instead of saving the whole form.
  var SKIP_TYPES = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'range', 'color'];

  document.addEventListener('keydown', function (e) {{
    var isSaveCombo = (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's';
    var isPlainEnter = e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (!isSaveCombo && !isPlainEnter) return;

    var target = e.target;

    if (target && target.dataset && target.dataset.enterAction) {{
      e.preventDefault();
      try {{ new Function(target.dataset.enterAction)(); }} catch (err) {{}}
      return;
    }}

    if (isSaveCombo) {{
      e.preventDefault(); // stop the browser's own "Save Page" dialog
      triggerSave(target);
      return;
    }}

    // Plain Enter: leave textareas (new line), dropdowns, and
    // checkboxes/buttons/etc. to their normal behaviour, and skip any
    // live filter/search box (it drives its own Enter handling, or
    // none at all) so typing a search term never saves anything.
    var tag = target ? target.tagName : '';
    if (tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (target && target.isContentEditable) return;
    if (tag !== 'INPUT') return;
    var type = (target.type || 'text').toLowerCase();
    if (SKIP_TYPES.indexOf(type) !== -1) return;
    if ((target.id || '').toLowerCase().indexOf('search') !== -1) return;
    if (!target.closest('form')) return;

    e.preventDefault();
    triggerSave(target);
  }});
}})();

(function() {{
  var input = document.getElementById('globalSearch');
  var panel = document.getElementById('globalSearchResults');
  if (!input || !panel) return;

  // If the current page has its own live-filtering search box (the
  // quotations/customers/items/proforma/packing/salary list pages all do),
  // typing in the global box also drives that box, so the page's table
  // filters live too -- no need to duplicate each page's own search logic
  // here.
  var LOCAL_INPUT_IDS = ['qSearchInput', 'custSearchInput', 'itemSearch',
    'piSearchInput', 'plSearchInput', 'salSearch'];

  function localInput() {{
    for (var i = 0; i < LOCAL_INPUT_IDS.length; i++) {{
      var el = document.getElementById(LOCAL_INPUT_IDS[i]);
      if (el) return el;
    }}
    return null;
  }}

  function syncLocal(value) {{
    var el = localInput();
    if (!el || el === input) return;
    el.value = value;
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  }}

  var ICONS = {{
    quotation: 'bi-file-earmark-text', customer: 'bi-people',
    item: 'bi-box-seam', proforma: 'bi-receipt', packing: 'bi-box-seam'
  }};
  var GROUP_LABELS = {{
    quotation: 'Quotations', customer: 'Customers', item: 'Items',
    proforma: 'Proforma Invoices', packing: 'Packing Lists'
  }};

  function renderResults(results) {{
    if (!results.length) {{
      panel.innerHTML = '<div class="global-search-empty">No matches found</div>';
      panel.classList.remove('d-none');
      return;
    }}
    var byType = {{}};
    results.forEach(function(r) {{
      (byType[r.type] = byType[r.type] || []).push(r);
    }});
    var html = '';
    Object.keys(byType).forEach(function(type) {{
      html += '<div class="global-search-group-label">' + GROUP_LABELS[type] + '</div>';
      byType[type].forEach(function(r) {{
        html += '<a class="global-search-item" href="' + r.url + '">' +
          '<i class="bi ' + (ICONS[type] || 'bi-search') + ' gs-icon"></i>' +
          '<div class="min-w-0"><div class="gs-title">' + escapeHtml(r.title) + '</div>' +
          (r.subtitle ? '<div class="gs-subtitle">' + escapeHtml(r.subtitle) + '</div>' : '') +
          '</div></a>';
      }});
    }});
    panel.innerHTML = html;
    panel.classList.remove('d-none');
  }}

  function escapeHtml(s) {{
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }}

  var debounceTimer = null;
  var lastResults = [];

  function runSearch(value) {{
    if (value.length < 2) {{
      panel.classList.add('d-none');
      lastResults = [];
      return;
    }}
    fetch('/api/search?q=' + encodeURIComponent(value))
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        lastResults = data.results || [];
        renderResults(lastResults);
      }})
      .catch(function() {{ panel.classList.add('d-none'); }});
  }}

  input.addEventListener('input', function(e) {{
    var value = e.target.value;
    syncLocal(value);
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function() {{ runSearch(value); }}, 250);
  }});

  input.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{
      e.preventDefault();
      clearTimeout(debounceTimer);
      if (lastResults.length) {{
        window.location.href = lastResults[0].url;
      }}
    }} else if (e.key === 'Escape') {{
      panel.classList.add('d-none');
      input.blur();
    }}
  }});

  input.addEventListener('focus', function() {{
    if (lastResults.length && input.value.trim().length >= 2) {{
      panel.classList.remove('d-none');
    }}
  }});

  document.addEventListener('click', function(e) {{
    if (!panel.contains(e.target) && e.target !== input) {{
      panel.classList.add('d-none');
    }}
  }});
}})();
</script>
</body>
</html>'''


