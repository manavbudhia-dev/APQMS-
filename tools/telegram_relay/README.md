# Telegram relay (Cloudflare Worker)

Some shared hosting (e.g. MilesWeb) blocks outbound connections to
`api.telegram.org`, and the bot then fails with `[Errno 101] Network is
unreachable`. Incoming messages still work (Telegram → your site); only the
app's calls to Telegram are blocked. This Worker carries those calls:

    app ──HTTPS──▶ https://<name>.<subdomain>.workers.dev/bot<TOKEN>/<method>
                   (header X-Relay-Key)  ──▶ api.telegram.org

Incoming direction: if the host also blocks Telegram's servers from reaching
the site (Check webhook shows "Connection timed out"), the app registers the
webhook at `<relay>/webhook` automatically when `APQMS_TELEGRAM_API_URL` is
set. The Worker forwards those POSTs to its `WEBHOOK_TARGET` variable
(`https://qms.aypeeindustries.com/api/v1/telegram/webhook`); the app still
checks Telegram's secret header itself.

## Deployed instance
`https://apqms-telegram-relay.webberzweb.workers.dev` (Cloudflare account of the owner).

## App settings (`.env`, then restart the app)
```
APQMS_TELEGRAM_API_URL=https://apqms-telegram-relay.webberzweb.workers.dev
APQMS_TELEGRAM_RELAY_KEY=<same value as the Worker's RELAY_KEY secret>
```

## Re-deploy / new key
Cloudflare dashboard → Workers & Pages → Create → Worker → paste `worker.js`
→ Deploy. Then Settings → Variables and Secrets → add secret `RELAY_KEY` and
text variable `WEBHOOK_TARGET` = `https://<your-site>/api/v1/telegram/webhook`.

## Safety
- Requests without the right `X-Relay-Key` get 403, so it is not an open proxy.
- Only Bot API paths (`/bot<id>:<token>/...`) are forwarded, and only to api.telegram.org.
- The bot token passes through Cloudflare (same as any HTTPS proxy). Free plan: 100,000 requests/day.
