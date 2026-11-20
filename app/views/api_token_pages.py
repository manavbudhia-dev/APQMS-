"""
Settings -> API Tokens: create / revoke the bearer tokens used by the
assistant JSON API (/api/v1/*), AI-assistant apps and `apqms_cli.py`.

Each user manages their own tokens; an admin also sees everyone's. A new
token is displayed exactly once — only its hash is stored.
"""
from app.assistant import tokens
from app.assistant.telegram import allowed_chats, bot_token, webhook_secret
from utils.http_utils import (consume_csrf_token, esc, html_response, new_csrf_token, redirect)


class ApiTokenPagesMixin:

    def pg_api_tokens(self, user_id, new_token=None, msg=''):
        is_admin = self._is_admin(user_id)
        rows = tokens.list_tokens(None if is_admin else user_id)
        csrf = new_csrf_token()
        base = self._api_base_url()

        new_html = ''
        if new_token:
            new_html = f'''
<div class="alert alert-success">
  <strong>Token ban gaya — ise abhi copy kar lein, dobara nahi dikhega:</strong>
  <div class="mt-2"><code style="font-size:1rem;user-select:all;word-break:break-all">{esc(new_token)}</code></div>
</div>'''
        if msg:
            new_html += f'<div class="alert alert-info">{esc(msg)}</div>'

        body_rows = ''
        for r in rows:
            state = '<span class="badge bg-secondary">Revoked</span>' if r['revoked'] else \
                '<span class="badge bg-success">Active</span>'
            action = '' if r['revoked'] else f'''
<form method="post" action="/settings/api-tokens/{r['id']}/revoke" style="display:inline"
      onsubmit="return confirm('Revoke this token? Apps using it will stop working.')">
  <input type="hidden" name="csrf_token" value="{esc(csrf)}">
  <button class="btn btn-sm btn-outline-danger">Revoke</button>
</form>'''
            body_rows += (f"<tr><td>{esc(r['name'])}</td><td><code>{esc(r['token_hint'] or '')}</code></td>"
                          f"<td>{esc(r['username'])}</td><td>{esc(r['created_at'] or '')}</td>"
                          f"<td>{esc(r['last_used_at'] or '—')}</td><td>{state}</td><td>{action}</td></tr>")
        if not body_rows:
            body_rows = '<tr><td colspan="7" class="text-muted">Abhi koi token nahi hai.</td></tr>'

        tg = ''
        if is_admin:
            def ok(v):
                return '✅' if v else '❌'
            tg = f'''
<div class="form-section mt-4">
  <h5><i class="bi bi-telegram"></i> Telegram bot status</h5>
  <ul class="mb-0">
    <li>{ok(bot_token())} APQMS_TELEGRAM_BOT_TOKEN</li>
    <li>{ok(webhook_secret())} APQMS_TELEGRAM_WEBHOOK_SECRET</li>
    <li>{ok(allowed_chats())} APQMS_TELEGRAM_ALLOWED_CHATS ({len(allowed_chats())} chat)</li>
  </ul>
  <small class="text-muted">Setup steps: ASSISTANT_API.md (project folder).</small>
</div>'''

        content = f'''
{new_html}
<div class="form-section">
  <h5><i class="bi bi-plug"></i> New API token</h5>
  <p class="text-muted mb-2">AI assistant app, script ya CLI ko connect karne ke liye. Token se jo bhi
  quotation / proforma banegi, wo aapke naam se banegi.</p>
  <form method="post" action="/settings/api-tokens" class="d-flex gap-2 flex-wrap">
    <input type="hidden" name="csrf_token" value="{esc(csrf)}">
    <input name="name" class="form-control" style="max-width:320px" placeholder="e.g. My phone assistant" required maxlength="100">
    <button class="btn btn-primary"><i class="bi bi-plus-lg"></i> Create token</button>
  </form>
  <div class="mt-3 small text-muted">
    API base URL: <code>{esc(base)}/api/v1</code> &nbsp;·&nbsp;
    OpenAPI spec: <a href="/api/v1/openapi.json" target="_blank"><code>{esc(base)}/api/v1/openapi.json</code></a>
  </div>
</div>
<div class="form-section mt-4">
  <h5>Tokens</h5>
  <div class="table-responsive">
  <table class="table table-sm align-middle mb-0">
    <thead><tr><th>Name</th><th>Token</th><th>User</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
  </div>
</div>
{tg}'''
        return self._layout('API Tokens', 'API Tokens', content, 'settings')

    def handle_api_token_create(self, form, user_id):
        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*redirect('/settings/api-tokens'))
        _, token = tokens.create_token(user_id, form.get('name', ''))
        return self.send(*html_response(self.pg_api_tokens(user_id, new_token=token)))

    def handle_api_token_revoke(self, token_id, form, user_id):
        if consume_csrf_token(form.get('csrf_token', '')):
            tokens.revoke_token(token_id, None if self._is_admin(user_id) else user_id)
        return self.send(*redirect('/settings/api-tokens'))
