# File Guide — where do I make my change?

> Deploying to cPanel shared hosting: see **`DEPLOY_CPANEL.md`** (auto-deploy on every merge to `main`).

Quick lookup: find what you want to change on the left, edit the file on the right.

## "I want to change how a page looks or what it shows"

| Page / feature | File |
|---|---|
| Login page | `auth_pages.py` |
| Register page | `auth_pages.py` |
| Dashboard | `dashboard_pages.py` |
| 404 / "Page not found" | `dashboard_pages.py` |
| Customers list | `customer_pages.py` |
| Customer → linked products page | `customer_pages.py` |
| Items catalog | `item_pages.py` |
| Quotations: list / new / edit / detail / PDF | `quotation_pages.py` |
| Proforma Invoices: list / new / edit / detail / PDF | `proforma_pages.py` |
| Packing Lists: list / new / edit / detail / PDF | `packing_pages.py` |
| Salary / Employees / Attendance | `salary_pages.py` |
| Business Metrics (forecast / pricing / anomalies) | `metrics_pages.py` (page) + `app/ml/*.py` (models) |
| Scan & Import (OCR): upload / review / import list | `ocr_pages.py` (page) + `app/ocr/*.py` (reading the document) |

## "I want to change how a form saves / a button submits"

Same file as the page it belongs to — the `handle_*` function that saves the
form lives right next to the `pg_*` function that renders it.

| Form action | File |
|---|---|
| Login / Register submit | `auth_pages.py` |
| Add / edit customer, assign items to a customer | `customer_pages.py` |
| Add / edit item | `item_pages.py` |
| Create / edit quotation | `quotation_pages.py` |
| Create / edit proforma invoice | `proforma_pages.py` |
| Create / edit packing list | `packing_pages.py` |
| Add / edit employee, save attendance | `salary_pages.py` |
| Recalculate Business Metrics (admin only) | `metrics_pages.py` |
| Upload a document / save reviewed data / create a quotation from it | `ocr_pages.py` |

## "I want to work on Photo Quote (photo of a hand-edited sheet -> quotation)"

`app/vision/` (prompt, JSON schema, Claude call, validation, audit store) and
`app/views/vision_pages.py` (upload / review / confirm). Optional
dependency: `requirements-vision.txt` + `ANTHROPIC_API_KEY`. See
**`PHOTO_QUOTE.md`**.

## "I want to work on the AI assistant / Telegram bot / API / CLI"

Everything lives in `app/assistant/` (+ `apqms_cli.py` and the Settings →
API Tokens page in `app/views/api_token_pages.py`). Setup, bot commands and
the API reference are in **`ASSISTANT_API.md`**.

## "I want to work on Business Metrics (forecasting / pricing / anomalies)"

This is the one part of the app with an optional dependency — see
`requirements-ml.txt`. `app/ml/common.py` has the shared scikit-learn-loading
and caching helpers; `app/ml/forecasting.py`, `pricing.py`, and
`anomalies.py` each hold one model. All three are computed together by the
admin-only "Refresh Metrics" button (`handle_metrics_refresh` in
`metrics_pages.py`) and cached to `app/ml/_cache/*.json` — viewing the page
never itself triggers training. If scikit-learn isn't installed, the page
shows an install hint instead of erroring; the rest of the app is
unaffected either way.

## "I want to work on Scan & Import (reading enquiries / purchase orders)"

The second part of the app with an optional dependency — see
`requirements-ocr.txt` (it also needs the Tesseract program installed on the
server; the file explains how). Upload a scanned PDF, photo, Word file or
digital PDF at `/ocr`, check what was read on the review page, then **Save
data** or **Create quotation** (a Draft, made through the same code as the
manual New Quotation form). Nothing becomes a quotation until a person has
seen the extracted rows.

| To change… | File |
|---|---|
| The pages, review form, save / create-quotation / delete actions | `app/views/ocr_pages.py` |
| Reading a file: PDF text layer vs OCR, Word, images, table rebuilding | `app/ocr/engine.py` |
| Finding customer details and line items in the text (heuristics) | `app/ocr/parser.py` |
| Matching items to the catalog and the customer to existing customers | `app/ocr/matching.py` |
| File storage, background processing, database reads/writes | `app/ocr/service.py` |
| Our own name/GSTIN/phones (so they are never mistaken for the customer) | `OWN_*` at the top of `app/ocr/parser.py` |
| Sample documents and tests | `tests/make_fixtures.py`, `tests/test_ocr_*.py` |

Run the tests with `python -m unittest discover tests` (tests that need
Tesseract are skipped if it isn't installed). Uploaded originals are kept in
`ocr_uploads/` next to the database (set `QUOTATION_OCR_DIR` to move it) and
are only reachable through the logged-in `/ocr/<id>/file` route. Tuning
knobs, all optional environment variables: `OCR_MAX_UPLOAD_MB` (15),
`OCR_MAX_PAGES` (20), `OCR_LANG` (`eng`), `OCR_TWO_PASS` (`1`; `0` is ~2x
faster but finds fewer table cells), `OCR_MAX_CONCURRENT` (2),
`TESSERACT_CMD`, `OCR_OWN_KEYWORDS`.

Known limits: reading is heuristic, which is why the review step exists. Only
the first items table in a document is read (a repeated header row on later
pages is handled). Handwriting is not supported. Very poor scans can lose
individual table cells — such rows come through flagged ("No quantity found",
"Poorly scanned") rather than being dropped, and the raw text is always
available on the review page to compare against.

Why it is built the way it is (learned by measuring, not assumed): scans are
OCR'd at their **native resolution** — enlarging them made Tesseract lose
most quantity/unit cells in ruled tables; Tesseract's `--psm 6` mode also
dropped those cells, so two passes (`--psm 3` + `--psm 11`) are merged
instead; and table rows are rebuilt from word positions rather than trusting
Tesseract's own line grouping.

## "I want to add a new URL / route, or change a delete/status endpoint"

→ **`routing.py`** — this is `do_GET` and `do_POST`. Every URL the server
responds to (including the small inline delete/status routes that don't have
their own `handle_*` function) is listed here.

## "I want to change the sidebar, page layout, or the shared HTML shell"

→ **`base_mixin.py`** — this has `_layout()` (the `<html>` wrapper, sidebar,
nav links) plus low-level `send()` and static-file serving. Change this if
you're adding a new nav item, changing the sidebar, or editing the CSS/JS
`<link>`/`<script>` tags in the page shell.

## "I want to change the port, or where static files are served from"

→ **`config.py`** — just `PORT` and `BASE`.

## "I want to change number/date formatting, HTML-escaping, or session behavior"

→ **`http_utils.py`** — `esc()`, `esc_nl()`, `fmt_rupee()`, the in-memory
session store (`get_session`/`create_session`), form parsing, and the
response-tuple helpers (`redirect`, `json_response`, `html_response`,
`pdf_response`).

## "I want to change the database, or PDF generation itself"

These weren't part of the split — they're your existing files, untouched:

- `database.py` — schema, queries, password hashing, number generators
- `pdf_generator.py` — quotation PDF layout
- `proforma_generator.py` — proforma invoice PDF layout

## "I want to change server startup / add a new page module"

→ **`server.py`** — the entry point. It only does two things: combines all
the mixin files into one `QuotationHandler` class, and starts the server. If
you create a brand-new module (say `reports_pages.py`), import it here and
add it to the `QuotationHandler(...)` class list.

---

### Rule of thumb
Ask "what feature is this?" (quotations, proforma, packing, customers, items,
salary, auth, dashboard) → that's your file. If it's about *routing a URL*,
*the page shell/sidebar*, or *a low-level helper*, it's `routing.py`,
`base_mixin.py`, or `http_utils.py` instead.

## Database backups on the server

Settings → Database Backup (admin only) can keep copies of the database **on the server** as well as downloading them:

* **Download Backup (.db)** — with "Also keep a copy on the server when I download" ticked, the same snapshot is saved on the server too.
* **Save on Server** — saves a copy on the server without downloading.
* Stored copies are listed on the same page (download / delete).

Files go to `server_backups/` next to `quotation.db` (owner-only permissions). Set the `QUOTATION_BACKUP_DIR` environment variable to use another folder. Only the newest 30 are kept (`MAX_SERVER_BACKUPS` in `app/views/settings_pages.py`). Note these copies live on the same disk as the live database, so keep a downloaded copy elsewhere too.

### Password-protecting a backup

Type a password (twice, min. 8 characters) in the Database Backup card before clicking **Download Backup** or **Save on Server**. The backup — and the server copy, if you ticked it — is then encrypted and saved as `.db.enc`. To restore it, choose the file and enter the same password in the Restore box. Leave the password boxes blank for a normal `.db`. Plain `.db` backups (including old ones) still restore without a password.

**A lost password cannot be recovered** — the backup is unreadable without it. Encryption code lives in `utils/backup_crypto.py` (standard library only; no extra installs).
