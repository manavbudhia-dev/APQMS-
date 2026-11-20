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


def check_webhook_secret(header_value):
    secret = webhook_secret()
    return bool(secret) and hmac.compare_digest(secret, (header_value or '').strip())


# ── Bot API calls ────────────────────────────────────────────
def _call(method, params=None, timeout=30):
    url = f'{API}/bot{bot_token()}/{method}'
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
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
    req = urllib.request.Request(f'{API}/bot{bot_token()}/sendDocument', data=b''.join(parts),
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def send_text(chat_id, text):
    for i in range(0, max(len(text), 1), MAX_MESSAGE):
        _call('sendMessage', {'chat_id': chat_id, 'text': text[i:i + MAX_MESSAGE] or '-'})


def send_reply(chat_id, reply):
    send_text(chat_id, reply.text)
    if reply.pdf:
        _send_document(chat_id, reply.pdf_name or 'document.pdf', reply.pdf)


# ── update handling (shared by webhook + polling) ────────────
def handle_update(update):
    """Process one Telegram update dict. Never raises."""
    try:
        msg = update.get('message') or update.get('edited_message') or {}
        text = msg.get('text') or msg.get('caption') or ''
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        if chat_id is None or not text:
            return
        if str(chat_id) not in allowed_chats():
            send_text(chat_id, f'Aap is bot ke liye authorised nahi hain.\nAapka chat id: {chat_id}\n'
                               'Admin ise APQMS_TELEGRAM_ALLOWED_CHATS mein add kare.')
            return
        user = bot_user()
        if user is None:
            send_text(chat_id, 'Server par APQMS_TELEGRAM_USER wala user nahi mila / inactive hai.')
            return
        try:
            _call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'}, timeout=10)
        except Exception:
            pass
        reply = bot.handle(text, user, chat_key=f'tg:{chat_id}')
        send_reply(chat_id, reply)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        try:
            send_text(update['message']['chat']['id'], '⚠️ Kuch galat ho gaya. Dobara try karein.')
        except Exception:
            pass


def set_webhook(base_url):
    if not webhook_secret():
        raise SystemExit('Set APQMS_TELEGRAM_WEBHOOK_SECRET first (any long random string).')
    url = base_url.rstrip('/') + '/api/v1/telegram/webhook'
    return _call('setWebhook', {'url': url, 'secret_token': webhook_secret(),
                                'allowed_updates': ['message', 'edited_message'],
                                'drop_pending_updates': True})


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
