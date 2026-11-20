"""
Settings -> API Tokens: create / revoke the bearer tokens used by the
assistant JSON API (/api/v1/*), AI-assistant apps and `apqms_cli.py`.

Each user manages their own tokens; an admin also sees everyone's. A new
token is displayed exactly once — only its hash is stored.
"""
from app.assistant import tokens
from app.assistant import telegram
from app.assistant.telegram import allowed_chats, bot_token, webhook_secret
from utils.http_utils import (consume_csrf_token, esc, html_response, new_csrf_token, redirect)


class ApiTokenPagesMixin:

    def _telegram_base(self):
        """Public HTTPS address for the webhook. Behind cPanel/Passenger the
        request often looks like plain http, but Telegram needs https."""
        base = self._api_base_url()
        host = base.split('://', 1)[-1]
        if base.startswith('http://') and not host.startswith(('localhost', '127.')):
            base = 'https://' + host
        return base

    def pg_api_tokens(self, user_id, new_token=None, msg='', tg_report=None):
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
        report_html = ''
        if tg_report:
            icon = {True: '✅', False: '❌', None: 'ℹ️'}
            report_html = '<div class="alert alert-light border small mt-2 mb-0">' + '<br>'.join(
                f'{icon[o]} {esc(t)}' for o, t in tg_report) + '</div>'
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
  {report_html}
  <div class="d-flex gap-2 flex-wrap mt-2">
    <form method="post" action="/settings/telegram/check">
      <input type="hidden" name="csrf_token" value="{esc(csrf)}">
      <button class="btn btn-sm btn-outline-primary"><i class="bi bi-search"></i> Check webhook</button>
    </form>
    <form method="post" action="/settings/telegram/register"
          onsubmit="return confirm('Webhook ko {esc(telegram.webhook_url(self._telegram_base()))} par register karein?')">
      <input type="hidden" name="csrf_token" value="{esc(csrf)}">
      <button class="btn btn-sm btn-primary" {"disabled" if not (bot_token() and webhook_secret()) else ""}>
        <i class="bi bi-link-45deg"></i> Register webhook</button>
    </form>
  </div>
  <small class="text-muted">Register: <code>{esc(telegram.webhook_url(self._telegram_base()))}</code> (HTTPS hona chahiye).
  Setup steps: ASSISTANT_API.md.</small>
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

    def handle_telegram_check(self, form, user_id):
        if not self._is_admin(user_id) or not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*redirect('/settings/api-tokens'))
        report = telegram.diagnose(self._telegram_base())
        return self.send(*html_response(self.pg_api_tokens(user_id, tg_report=report)))

    def handle_telegram_register(self, form, user_id):
        if not self._is_admin(user_id) or not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*redirect('/settings/api-tokens'))
        base = self._telegram_base()
        if not base.startswith('https://'):
            report = [(False, f'Site ka address {base} HTTPS nahi hai. Telegram sirf HTTPS webhook leta hai. '
                              '.env mein APQMS_PUBLIC_URL=https://aapki-site set karein.')]
        else:
            try:
                res = telegram.set_webhook(base)
                report = [(bool(res.get('ok')), 'Telegram: ' + str(res.get('description') or res))]
            except SystemExit as e:
                report = [(False, str(e))]
            except Exception as e:
                detail = getattr(e, 'read', lambda: b'')()
                report = [(False, f'Register fail: {e} {detail.decode("utf-8", "replace")[:300] if detail else ""}')]
            report += telegram.diagnose(base)
        return self.send(*html_response(self.pg_api_tokens(user_id, tg_report=report)))
