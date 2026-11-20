// Cloudflare Worker: relay for the Telegram Bot API.
// For hosts that block outbound connections to api.telegram.org (some
// shared hosting does). The app sends https://<worker>/bot<TOKEN>/<method>
// with header X-Relay-Key; the Worker forwards it to api.telegram.org.
//
// Secret (Worker setting): RELAY_KEY — same value as APQMS_TELEGRAM_RELAY_KEY
// in the app's .env. Requests without it get 403, so this is not an open proxy.
//
// Incoming direction too: when the host also blocks Telegram's servers from
// reaching the site, register the webhook at https://<worker>/webhook. The
// Worker passes the POST (with Telegram's X-Telegram-Bot-Api-Secret-Token
// header, which the app checks) to WEBHOOK_TARGET (Worker variable), e.g.
// https://your-site/api/v1/telegram/webhook.
const BOT_PATH = /^\/(file\/)?bot\d+:[A-Za-z0-9_-]+\/[A-Za-z0-9_./-]*$/;

function same(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/' || url.pathname === '/health') {
      return new Response('telegram relay ok\n');
    }
    if (url.pathname === '/webhook') {
      if (request.method !== 'POST' || !env.WEBHOOK_TARGET) {
        return new Response('not found\n', { status: 404 });
      }
      const fwd = { 'content-type': request.headers.get('content-type') || 'application/json' };
      const secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
      if (secret) fwd['X-Telegram-Bot-Api-Secret-Token'] = secret;
      const r = await fetch(env.WEBHOOK_TARGET, { method: 'POST', headers: fwd, body: request.body });
      return new Response(await r.text(), { status: r.status, headers: { 'content-type': 'application/json' } });
    }
    if (!env.RELAY_KEY || !same(request.headers.get('X-Relay-Key') || '', env.RELAY_KEY)) {
      return new Response(JSON.stringify({ ok: false, description: 'relay: bad or missing X-Relay-Key' }),
        { status: 403, headers: { 'content-type': 'application/json' } });
    }
    if (!BOT_PATH.test(url.pathname)) {
      return new Response(JSON.stringify({ ok: false, description: 'relay: not a Bot API path' }),
        { status: 404, headers: { 'content-type': 'application/json' } });
    }
    const headers = {};
    const ct = request.headers.get('content-type');
    if (ct) headers['content-type'] = ct;
    const hasBody = !['GET', 'HEAD'].includes(request.method);
    return fetch('https://api.telegram.org' + url.pathname + url.search, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
    });
  },
};
