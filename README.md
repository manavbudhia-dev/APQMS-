# File Guide — where do I make my change?

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
