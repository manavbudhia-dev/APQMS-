// Cloudflare Worker: relay for the Telegram Bot API.
// For hosts that block outbound connections to api.telegram.org (some
// shared hosting does). The app sends https://<worker>/bot<TOKEN>/<method>
// with header X-Relay-Key; the Worker forwards it to api.telegram.org.
//
// Secret (Worker setting): RELAY_KEY — same value as APQMS_TELEGRAM_RELAY_KEY
// in the app's .env. Requests without it get 403, so this is not an open proxy.
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
