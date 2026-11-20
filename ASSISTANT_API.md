# AI Assistant / Bot / CLI — Setup Guide

Mobile se bot ko message bhejo → quotation / proforma ban jaati hai, PDF wapas aa jaata hai.

Teen raaste hain, teeno same engine use karte hain (`app/assistant/`):

| Raasta | Kab use karein |
|---|---|
| **Telegram bot** | Seedha phone se chat. Sabse aasaan. |
| **REST API** (`/api/v1/*`) | Apna AI assistant app (ChatGPT custom GPT, n8n, Make, iOS Shortcuts, koi bhi app jo HTTP call kar sake) |
| **CLI** (`apqms_cli.py`) | Server / laptop ke terminal se, scripts se |

Nayi quotation / proforma hamesha **Draft** banti hai (jab tak aap `status` na bolein) — app mein dekh kar Sent karein.

---

## 1. Bot ko kya bol sakte hain

```
quotation Krishna Sugar Mills
10 mtr roller chain 25.4 @ 450
bearing 6205 x 5
grease 5 kg @ 300
subject: Spares for season
```

```
proforma Krishna Sugar: 10 mtr chain 25.4 @450, 5 nos bearing 6205
po: 4512
```

| Message | Kya hoga |
|---|---|
| `AQ-2026-012 ka proforma bana do` | Quotation se proforma (same items/rates) |
| `isko proforma bana do` | Abhi-abhi bani quotation ka proforma |
| `AQ-2026-012 pdf` / `pdf bhejo` | PDF bhejega |
| `AQ-2026-012` | Details + total |
| `AQ-2026-012 status sent` / `confirm` / `cancel` | Status change |
| `quotations` / `recent proforma` / `confirmed quotations` | Last 10 ki list |
| `customer krishna` | Customer search |
| `item chain 25.4` / `rate of grease` | Catalog item + rate |
| `add customer Shree Ram Sugars` (agli lines: `phone: …`, `gstin: …`, `contact: …`) | Naya customer |
| `summary` | Is mahine ka hisaab |
| `help` | Saare examples |

**Rules:**
- Pehli line: `quotation` / `proforma` + customer naam. Customer naam ka ek hissa bhi chalega (`krishna`), agar wo ek hi customer se match kare. Kai match hon to bot poochega.
- Har agli line (ya comma se alag) ek item: `qty unit naam @ rate`. Order flexible hai: `bearing 6205 x 5 @ 250`, `grease 5 kg`, `qty 5 chain`.
- Rate na likhein to **catalog rate** lagega. Catalog mein item na mile to rate dena zaroori hai (tab item free-text mein jaayega).
- Units: mtr, pcs, kg, nos, set, ltr, pair, roll, bag, ton, ft, box.
- Fields (alag line par): `subject:`, `notes:`, `po:` / `order no:`, `order date:`, `enquiry:`, `status:`, `customer:`.

---

## 2. Telegram bot setup (≈10 minute)

1. Telegram mein **@BotFather** kholo → `/newbot` → naam do → **token** milega (`123456:ABC...`).
2. Server par ye environment variables set karo (cPanel: *Setup Python App → Environment variables*; systemd/pm2: service env):

   | Variable | Value |
   |---|---|
   | `APQMS_TELEGRAM_BOT_TOKEN` | BotFather wala token |
   | `APQMS_TELEGRAM_WEBHOOK_SECRET` | koi bhi lamba random text (e.g. `openssl rand -hex 24`) |
   | `APQMS_TELEGRAM_ALLOWED_CHATS` | aapka chat id (step 4 mein milega). Kai log: `111,222` |
   | `APQMS_TELEGRAM_USER` | app ka username jiske naam se documents banein (default `admin`) |

3. App restart karo, phir webhook register karo (server par, ek baar):
   ```bash
   python3 apqms_cli.py telegram set-webhook https://aapki-site.com
   ```
   (Site **HTTPS** par honi chahiye. Public URL nahi hai? Webhook ki jagah `python3 apqms_cli.py telegram poll` chala do — ye bina public URL ke kaam karta hai, bas process chalta rehna chahiye.)
4. Apne bot ko Telegram par `hi` bhejo. Bot jawab dega: *"Aapka chat id: 123456789"*. Ye id `APQMS_TELEGRAM_ALLOWED_CHATS` mein daalo, app restart karo.
5. Ab `help` bhejo — ready! Settings → **API Tokens** page par admin ko Telegram config ka status (✅/❌) dikhta hai.

Security: sirf allowed chat ids hi bot use kar sakte hain; baaki ko sirf unka chat id dikhta hai. Webhook request bina secret header ke reject hoti hai.

---

## 3. Apna AI assistant app connect karna (REST API)

### Token banao
App mein login → upar profile menu → **API Tokens** → naam do → *Create token*. Token (`apqms_...`) sirf ek baar dikhega — copy kar lo. (Ya server par: `python3 apqms_cli.py token create --user admin --name "my assistant"`.)

Har request mein header: `Authorization: Bearer apqms_...`

### Sabse aasaan: ek hi endpoint — `/api/v1/chat`
Assistant ko bas user ka message forward karna hai, bot jaisa hi jawab milega:

```bash
curl -X POST https://aapki-site.com/api/v1/chat \
  -H "Authorization: Bearer apqms_xxx" -H "Content-Type: application/json" \
  -d '{"message": "quotation Krishna Sugar: 10 mtr chain 25.4 @ 450, 5 nos bearing 6205",
       "conversation_id": "mobile"}'
```
Response: `reply` (dikhane wala text), `document` (number, items, totals), `pdf_url`. `"include_pdf_base64": true` bhejoge to PDF bhi base64 mein aa jaayega. Same `conversation_id` rakho to "isko proforma bana do" jaise follow-up kaam karte hain.

### ChatGPT (Custom GPT) / doosre AI apps — tools ke roop mein
OpenAPI spec: `https://aapki-site.com/api/v1/openapi.json`

- **ChatGPT → Create a GPT → Configure → Actions → Import from URL** → upar wala URL. Authentication: *API Key → Bearer* → token paste. Instructions mein likho: *"Jab user quotation/proforma bole to createDocument use karo; customer/item confirm karne ke liye searchCustomers/searchItems; pehle preview:true se total dikhao, user haan bole tab save karo."*
- **n8n / Make / Zapier / iOS Shortcuts**: HTTP Request node → `POST /api/v1/chat` (ya structured endpoints neeche).

### Structured endpoints

| Method | Path | Kaam |
|---|---|---|
| GET | `/api/v1/health` | (bina token) check |
| GET | `/api/v1/openapi.json` | (bina token) spec |
| GET | `/api/v1/me` | token kiska hai |
| POST | `/api/v1/chat` | free-text command |
| GET | `/api/v1/summary` | counts / values |
| GET / POST | `/api/v1/customers?q=` | search / add customer |
| GET | `/api/v1/items?q=` | catalog search (rates ke saath) |
| GET | `/api/v1/quotations?q=&status=&limit=` | list (same for `/proformas`) |
| POST | `/api/v1/quotations` | create (same for `/proformas`) |
| GET | `/api/v1/quotations/{id ya AQ-2026-012}` | details + totals |
| GET | `/api/v1/quotations/{ref}/pdf` | PDF download |
| POST | `/api/v1/quotations/{ref}/status` | `{"status": "Sent"}` |
| POST | `/api/v1/quotations/{ref}/proforma` | quotation → proforma `{"order_no","order_date","mark_confirmed"}` |

Create body:
```json
{
  "customer": "Krishna Sugar",
  "subject": "Season spares",
  "items": [
    {"item": "roller chain 25.4", "quantity": 10},
    {"item": "bearing 6205", "quantity": 5, "rate": 240},
    {"item": "Custom guard plate", "quantity": 2, "rate": 1000, "unit": "Pcs."}
  ],
  "preview": false
}
```
- `customer`: naam (fuzzy), id, ya GSTIN. Ya `"new_customer": {"company_name": ..., "phone": ...}`.
- `item` text catalog se fuzzy-match hota hai (naam/description/spec/unit/rate wahin se). `item_id` do to exact.
- Proforma ke liye extra: `order_no`, `order_date`. Quotation ke liye: `enquiry_no`, `enquiry_date`, `customer_notes`.
- `"preview": true` → kuch save nahi hota, bas resolved items + total wapas (confirmation ke liye best).

Errors JSON mein aate hain: `{"error": "customer_not_found", "message": "...", "detail": {"suggestions": [...]}}` — assistant ye suggestions user ko dikha kar pooch sakta hai.

Browser-based app se direct call karni ho to `APQMS_API_CORS_ORIGINS=https://your-app.example` set karo.

---

## 4. CLI

Server par (direct database):
```bash
python3 apqms_cli.py chat "quotation Krishna Sugar: 10 mtr chain 25.4 @ 450" --save-pdf .
python3 apqms_cli.py shell                         # interactive, bot jaisa
python3 apqms_cli.py create quotation --customer "Krishna Sugar" \
    --item "roller chain 25.4|10" --item "bearing 6205|5|240" --subject "Spares" --save-pdf .
python3 apqms_cli.py create proforma --customer Balaji --item "sprocket 12T|3|500" --order-no PO-77
python3 apqms_cli.py convert AQ-2026-012 --order-no PO-4512 --confirm --save-pdf .
python3 apqms_cli.py list quotation --status Draft
python3 apqms_cli.py pdf AQ-2026-012 -o .
python3 apqms_cli.py token create --user admin --name "my phone"   # / token list / token revoke 3
```
`--item` format: `"naam|qty|rate|unit"` (rate, unit optional).

Kahin se bhi (API ke through):
```bash
export APQMS_URL=https://aapki-site.com APQMS_TOKEN=apqms_xxx
python3 apqms_cli.py chat "summary"
python3 apqms_cli.py list proforma
```

---

## 5. Files

| File | Kaam |
|---|---|
| `app/assistant/core.py` | Business logic (customer/item matching, create, convert, status, PDF) — web forms wale save helpers hi reuse karta hai |
| `app/assistant/parser.py` | Chat message → intent |
| `app/assistant/bot.py` | Intent → reply text + PDF |
| `app/assistant/api.py` | `/api/v1/*` endpoints + OpenAPI spec |
| `app/assistant/telegram.py` | Telegram webhook / polling |
| `app/assistant/tokens.py` | API tokens (sirf SHA-256 hash store hota hai) + chat context |
| `app/views/api_token_pages.py` | Settings → API Tokens page |
| `apqms_cli.py` | CLI |
| `tests/test_assistant.py` | Tests: `python3 -m unittest tests.test_assistant -v` |

Nayi tables (`api_tokens`, `assistant_context`) pehli baar use hone par apne aap ban jaati hain.
