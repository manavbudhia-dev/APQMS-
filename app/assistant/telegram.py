"""
Telegram bot: talk to APQMS from your phone.

Configuration (environment variables):
  APQMS_TELEGRAM_BOT_TOKEN       token from @BotFather (required)
  APQMS_TELEGRAM_ALLOWED_CHATS   comma-separated chat ids allowed to use the
                                 bot. Anyone else gets their chat id back
                                 (so you can add it) and nothing else.
  APQMS_TELEGRAM_USER            app username documents are created as
                                 (default: admin)
  APQMS_TELEGRAM_WEBHOOK_SECRET  secret Telegram must echo in the
                                 X-Telegram-Bot-Api-Secret-Token header
                                 (required for webhook mode)
  APQMS_TELEGRAM_API_URL         optional relay instead of https://api.telegram.org,
                                 for hosts that block Telegram (see
                                 tools/telegram_relay/README.md)
  APQMS_TELEGRAM_RELAY_KEY       sent as X-Relay-Key to that relay
  APQMS_TELEGRAM_WEBHOOK_URL     optional public webhook address. Default: the
                                 relay's /webhook when a relay is set, else
                                 <site>/api/v1/telegram/webhook

Two ways to run it:
  * webhook  — the server receives updates at POST /api/v1/telegram/webhook
               (needs a public HTTPS URL; register with
               `python3 apqms_cli.py telegram set-webhook https://your.site`)
  * polling  — `python3 apqms_cli.py telegram poll` on any machine that can
               reach the database (no public URL needed)
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid

from app.assistant import bot, core

API = 'https://api.telegram.org'
# Webhook updates are handled on a background thread so a slow step (AI
# reading a pasted sheet takes 20-40 s) never makes Telegram time out and
# re-deliver the same message. Tests switch this off.
ASYNC_WEBHOOK = True
MAX_MESSAGE = 4000          # Telegram's limit is 4096 characters


def bot_token():
    return os.environ.get('APQMS_TELEGRAM_BOT_TOKEN', '').strip()


def webhook_secret():
    return os.environ.get('APQMS_TELEGRAM_WEBHOOK_SECRET', '').strip()


def allowed_chats():
    raw = os.environ.get('APQMS_TELEGRAM_ALLOWED_CHATS', '')
    return {c.strip() for c in raw.split(',') if c.strip()}


def bot_user():
    return core.user_by_username(os.environ.get('APQMS_TELEGRAM_USER', 'admin') or 'admin')


def api_base():
    return (os.environ.get('APQMS_TELEGRAM_API_URL') or API).strip().rstrip('/')


def webhook_url(site_base):
    """Address Telegram should POST updates to."""
    explicit = os.environ.get('APQMS_TELEGRAM_WEBHOOK_URL', '').strip()
    if explicit:
        return explicit
    if api_base() != API:            # host blocks Telegram both ways: come in through the relay too
        return api_base() + '/webhook'
    return site_base.rstrip('/') + '/api/v1/telegram/webhook'


def _headers(content_type):
    h = {'Content-Type': content_type, 'User-Agent': 'apqms-bot/1.0'}
    key = os.environ.get('APQMS_TELEGRAM_RELAY_KEY', '').strip()
    if key:
        h['X-Relay-Key'] = key
    return h


def check_webhook_secret(header_value):
    secret = webhook_secret()
    return bool(secret) and hmac.compare_digest(secret, (header_value or '').strip())


# ── Bot API calls ────────────────────────────────────────────
def _call(method, params=None, timeout=30):
    url = f'{api_base()}/bot{bot_token()}/{method}'
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers=_headers('application/json'))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _send_document(chat_id, filename, content, caption=''):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (('chat_id', str(chat_id)), ('caption', caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    safe = filename.replace('"', '').replace('\r', '').replace('\n', '')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe}"\r\n'
                 f'Content-Type: application/pdf\r\n\r\n'.encode() + content + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    req = urllib.request.Request(f'{api_base()}/bot{bot_token()}/sendDocument', data=b''.join(parts),
                                 headers=_headers(f'multipart/form-data; boundary={boundary}'))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def send_text(chat_id, text):
    for i in range(0, max(len(text), 1), MAX_MESSAGE):
        _call('sendMessage', {'chat_id': chat_id, 'text': text[i:i + MAX_MESSAGE] or '-'})


def send_reply(chat_id, reply):
    send_text(chat_id, reply.text)
    if reply.pdf:
        _send_document(chat_id, reply.pdf_name or 'document.pdf', reply.pdf)


# ── last update (shown by Check webhook) ─────────────────────
def _last_path():
    from app.config import BASE
    return os.path.join(BASE, 'tmp', 'telegram_last.json')


def record_last(**info):
    """Remember what happened to the latest update. Never raises."""
    try:
        info['at'] = time.strftime('%d %b %H:%M:%S UTC', time.gmtime())
        os.makedirs(os.path.dirname(_last_path()), exist_ok=True)
        with open(_last_path(), 'w', encoding='utf-8') as f:
            json.dump(info, f)
    except Exception:
        pass


def last_update():
    try:
        with open(_last_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _err_text(e):
    detail = ''
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = ' ' + e.read().decode('utf-8', 'replace')[:200]
        except Exception:
            pass
    return f'{type(e).__name__}: {e}{detail}'[:400]


# ── update handling (shared by webhook + polling) ────────────
def handle_update(update):
    """Process one Telegram update dict. Never raises."""
    chat_id, text = None, ''
    try:
        msg = update.get('message') or update.get('edited_message') or {}
        text = msg.get('text') or msg.get('caption') or ''
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        if chat_id is None or not text:
            record_last(chat_id=chat_id, text='', result='ignored (no text)')
            return
        if str(chat_id) not in allowed_chats():
            send_text(chat_id, f'Aap is bot ke liye authorised nahi hain.\nAapka chat id: {chat_id}\n'
                               'Admin ise APQMS_TELEGRAM_ALLOWED_CHATS mein add kare.')
            record_last(chat_id=chat_id, text=text[:40], result='not in ALLOWED_CHATS — chat id reply bheja')
            return
        user = bot_user()
        if user is None:
            send_text(chat_id, 'Server par APQMS_TELEGRAM_USER wala user nahi mila / inactive hai.')
            return
        try:
            _call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'}, timeout=10)
        except Exception:
            pass
        reply = bot.handle(text, user, chat_key=f'tg:{chat_id}',
                           notify=lambda t: send_text(chat_id, t))
        send_reply(chat_id, reply)
        record_last(chat_id=chat_id, text=text[:40], result='reply bheja')
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        record_last(chat_id=chat_id, text=text[:40], result='ERROR ' + _err_text(e))
        try:
            send_text(update['message']['chat']['id'], '⚠️ Kuch galat ho gaya. Dobara try karein.')
        except Exception:
            pass


def set_webhook(base_url):
    if not webhook_secret():
        raise SystemExit('Set APQMS_TELEGRAM_WEBHOOK_SECRET first (any long random string).')
    url = webhook_url(base_url)
    return _call('setWebhook', {'url': url, 'secret_token': webhook_secret(),
                                'allowed_updates': ['message', 'edited_message'],
                                'drop_pending_updates': True})


def diagnose(expected_base_url=''):
    """Plain-language checks of the bot setup, for the admin page.
    -> list of (ok: bool|None, text). Never raises."""
    out = []
    if not bot_token():
        return [(False, 'APQMS_TELEGRAM_BOT_TOKEN set nahi hai (.env mein daal kar app restart karein).')]
    via = api_base()
    if via != API:
        out.append((None, f'Relay ke through: {via}'))
    try:
        me = _call('getMe', timeout=15).get('result') or {}
        out.append((True, f"Bot token sahi hai: @{me.get('username')}"))
    except urllib.error.HTTPError as e:
        if e.code == 403 and via != API:
            return out + [(False, 'Relay ne request reject ki (403): APQMS_TELEGRAM_RELAY_KEY galat hai.')]
        return out + [(False, f'Telegram ne bot token reject kiya (HTTP {e.code}). @BotFather se token dobara copy karein.')]
    except Exception as e:
        if via == API:
            return [(False, f'Server se api.telegram.org tak connection nahi bana: {e}. Hosting Telegram block kar '
                            'rahi ho to relay use karein (APQMS_TELEGRAM_API_URL, tools/telegram_relay/README.md).')]
        return out + [(False, f'Server se relay ({via}) tak connection nahi bana: {e}')]
    try:
        info = _call('getWebhookInfo', timeout=15).get('result') or {}
    except Exception as e:
        return out + [(False, f'getWebhookInfo fail hua: {e}')]
    url = info.get('url') or ''
    expected = webhook_url(expected_base_url) if expected_base_url else ''
    if not url:
        out.append((False, 'Webhook register NAHI hai, isliye bot ko messages nahi milte. '
                           '"Register webhook" dabayein.'))
    elif expected and url != expected:
        out.append((False, f'Webhook galat URL par hai: {url} (hona chahiye {expected}). "Register webhook" dabayein.'))
    else:
        out.append((True, f'Webhook URL: {url}'))
    if info.get('last_error_message'):
        when = info.get('last_error_date')
        when = time.strftime('%d %b %H:%M UTC', time.gmtime(when)) if when else ''
        err = info['last_error_message']
        hint = ''
        if '403' in err:
            hint = (' → Server ka APQMS_TELEGRAM_WEBHOOK_SECRET us secret se alag hai jisse webhook register hua tha. '
                    '"Register webhook" dobara dabayein.')
        elif '404' in err:
            hint = ' → URL par app nahi mila; site address check karein.'
        elif '5' in err[:40] and 'Internal' in err:
            hint = ' → Server par error aaya; cPanel ka error log dekhein.'
        elif 'timed out' in err.lower() or 'connection refused' in err.lower():
            hint = (' → Telegram aapki site tak nahi pahunch pa raha (hosting firewall). Relay set karein '
                    '(APQMS_TELEGRAM_API_URL) aur "Register webhook" dobara dabayein — webhook relay ke through aayega.')
        if when and url and info.get('last_error_date', 0) < time.time() - 600:
            hint += ' (Ye error purana ho sakta hai; naya message bhej kar dobara check karein.)'
        out.append((False, f'Telegram ka last error ({when}): {err}{hint}'))
    elif url:
        out.append((True, 'Telegram ko webhook par koi error nahi mila.'))
    last = last_update()
    if last:
        bad = str(last.get('result', '')).startswith('ERROR')
        out.append((not bad,
                    f"Aakhri message server par aaya {last.get('at')} — chat {last.get('chat_id')}, "
                    f"\"{last.get('text')}\" → {last.get('result')}"))
    elif url:
        out.append((None, 'Server par abhi tak koi Telegram message nahi aaya. Register ke BAAD bot ko "hi" '
                          'bhejein, phir dobara Check webhook dabayein.'))
    pending = info.get('pending_update_count') or 0
    if pending:
        out.append((None, f'{pending} message(s) deliver hone ka wait kar rahe hain.'))
    if not webhook_secret():
        out.append((False, 'APQMS_TELEGRAM_WEBHOOK_SECRET set nahi hai — iske bina server har message 403 se reject karega.'))
    if not allowed_chats():
        out.append((None, 'APQMS_TELEGRAM_ALLOWED_CHATS khaali hai — bot sirf aapka chat id batayega. '
                          'Wo id .env mein daal kar restart karein.'))
    return out


def delete_webhook():
    return _call('deleteWebhook', {'drop_pending_updates': False})


def poll(stop_after=None):
    """Long-polling loop (no public URL needed). Ctrl+C to stop."""
    if not bot_token():
        raise SystemExit('Set APQMS_TELEGRAM_BOT_TOKEN first.')
    delete_webhook()                       # getUpdates refuses to work while a webhook is set
    me = _call('getMe').get('result', {})
    print(f"Polling as @{me.get('username')} — allowed chats: {sorted(allowed_chats()) or 'NONE'}")
    offset, handled = None, 0
    while stop_after is None or handled < stop_after:
        try:
            params = {'timeout': 50, 'allowed_updates': ['message', 'edited_message']}
            if offset is not None:
                params['offset'] = offset
            res = _call('getUpdates', params, timeout=60)
            for upd in res.get('result', []):
                offset = upd['update_id'] + 1
                handle_update(upd)
                handled += 1
        except KeyboardInterrupt:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f'network error: {e} — retrying in 5s', file=sys.stderr)
            time.sleep(5)
