#!/usr/bin/env python3
"""
AYPEE INDUSTRIES - Quotation Management System
=============================================
Standalone HTTP web server using only the Python standard library
(http.server) + reportlab for PDF generation. No Flask/Django.

This file is the thin entry point: it wires together the routing mixin
and all the page/handler mixins (each living in its own module) into a
single request-handler class, then starts the server.

Run with:  python3 server.py
Then open: http://localhost:5000

First run: if you haven't set ADMIN_PASSWORD in the environment,
database.py will generate one for the 'admin' account and print it once
to the console — watch for it the first time you start the server.
"""
import sys
import http.server

from app.config import PORT
from database import init_db

from app.routing import RoutingMixin
from app.models.base_mixin import BaseMixin
from app.views.auth_pages import AuthPagesMixin
from app.views.dashboard_pages import DashboardPagesMixin
from app.views.customer_pages import CustomerPagesMixin
from app.views.item_pages import ItemPagesMixin
from app.views.category_pages import CategoryPagesMixin
from app.views.quotation_pages import QuotationPagesMixin
from app.views.proforma_pages import ProformaPagesMixin
from app.views.packing_pages import PackingPagesMixin
from app.views.salary_pages import SalaryPagesMixin
from app.views.settings_pages import SettingsPagesMixin
from app.views.financial_year_pages import FinancialYearPagesMixin
from app.views.metrics_pages import BusinessMetricsPagesMixin
from app.views.ocr_pages import OcrPagesMixin
from app.views.api_token_pages import ApiTokenPagesMixin
from app.views.vision_pages import VisionPagesMixin
from app.assistant.api import AssistantApiMixin


# ─────────────────────────────────────────────────────────────
#  HTTP Handler — assembled from routing + page/handler mixins
# ─────────────────────────────────────────────────────────────
class QuotationHandler(
        RoutingMixin,
        AuthPagesMixin,
        DashboardPagesMixin,
        CustomerPagesMixin,
        ItemPagesMixin,
        CategoryPagesMixin,
        QuotationPagesMixin,
        ProformaPagesMixin,
        PackingPagesMixin,
        SalaryPagesMixin,
        SettingsPagesMixin,
        FinancialYearPagesMixin,
        BusinessMetricsPagesMixin,
        OcrPagesMixin,
        ApiTokenPagesMixin,
        VisionPagesMixin,
        AssistantApiMixin,
        BaseMixin,
        http.server.BaseHTTPRequestHandler):
    """
    Combines:
      - RoutingMixin        -> do_GET / do_POST (routes.py logic)
      - AuthPagesMixin       -> login / register / change-password
      - DashboardPagesMixin  -> dashboard / 404
      - CustomerPagesMixin   -> customers + their linked products
      - ItemPagesMixin       -> items catalog
      - CategoryPagesMixin   -> item categories (grouping items, e.g. per sugar mill)
      - QuotationPagesMixin  -> quotations (list/new/edit/detail/pdf)
      - ProformaPagesMixin   -> proforma invoices (list/new/edit/detail/pdf)
      - PackingPagesMixin    -> packing lists (list/new/edit/detail/pdf)
      - SalaryPagesMixin     -> employees, attendance, salary
      - SettingsPagesMixin   -> user profile, database backup, data export
      - FinancialYearPagesMixin -> switch/create the Indian financial year
        that scopes quotations/proforma invoices/packing lists + dashboard
        (items & customers are unaffected — see app/models/base_mixin.py
        _current_fy / _fy_switcher_html for the shared selection logic)
      - ApiTokenPagesMixin   -> Settings > API Tokens (for the assistant API)
      - VisionPagesMixin     -> Photo Quote: photo of a hand-edited sheet -> quotation
      - AssistantApiMixin    -> /api/v1/* JSON API, chat endpoint, Telegram webhook
      - BaseMixin            -> send(), serve_static(), _layout(), log_message()
    """
    pass


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()  # prints a generated admin password here on first run, if needed
    print("\n" + "=" * 54)
    print("  AYPEE INDUSTRIES - Quotation Management System")
    print(f"  Running on : http://localhost:{PORT}")
    print("=" * 54 + "\n")
    try:
        server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT),
                                                 QuotationHandler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
