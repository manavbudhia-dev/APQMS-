"""
AI-assistant / bot / CLI access to the Quotation Management System.

  core.py      business operations as plain functions (no HTTP): find
               customers & items, create quotations / proforma invoices,
               convert a quotation into a proforma, change status, PDFs.
  parser.py    turns a free-text chat message ("quotation Krishna Sugar:
               10 mtr chain 25.4 @ 450") into an intent + arguments.
  bot.py       message in -> reply (text + optional PDF) out. Shared by
               the Telegram bot, the /api/v1/chat endpoint and the CLI.
  tokens.py    API tokens (Bearer auth) for the JSON API.
  api.py       the /api/v1/* JSON endpoints (mixed into the HTTP handler).
  telegram.py  Telegram Bot API client (webhook + long-polling).

See ASSISTANT_API.md in the project root for setup and examples.
"""
