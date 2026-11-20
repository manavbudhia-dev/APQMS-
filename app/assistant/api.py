"""
/api/v1/* — JSON API for AI assistants, scripts and the CLI.

Auth: `Authorization: Bearer apqms_...` (create a token in the app under
Settings -> API Tokens, or `python3 apqms_cli.py token create`). Session
cookies are ignored here, and because a bearer token can't be sent by a
cross-site page on its own, these routes skip the browser same-origin
check the HTML forms rely on.

Mixed into the HTTP handler class (see server.py); the router hands every
/api/v1/ request to `_api_dispatch` before its cookie/session handling.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import traceback

from app.assistant import bot, core, telegram, tokens
from app.assistant.core import PROFORMA, QUOTATION, AssistantError

_KIND_SEG = {'quotations': QUOTATION, 'proformas': PROFORMA, 'proforma': PROFORMA}
_REF = r'([A-Za-z0-9\-]{1,40})'


def _cors_headers():
    origins = os.environ.get('APQMS_API_CORS_ORIGINS', '').strip()
    if not origins:
        return []
    return [('Access-Control-Allow-Origin', origins),
            ('Access-Control-Allow-Headers', 'Authorization, Content-Type'),
            ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
            ('Access-Control-Max-Age', '600')]


class AssistantApiMixin:

    # ── plumbing ─────────────────────────────────────────────
    def _api_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        return self.send(body, status, [('Content-Type', 'application/json; charset=utf-8')]
                         + _cors_headers())

    def _api_error(self, status, code, message, detail=None):
        out = {'error': code, 'message': message}
        if detail:
            out['detail'] = detail
        return self._api_json(out, status)

    def do_OPTIONS(self):
        if self.path.startswith('/api/v1/'):
            return self.send(b'', 204, _cors_headers())
        return self.send(b'', 405, [('Allow', 'GET, POST')])

    def _api_body(self, raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise AssistantError('Body must be valid JSON.', 'invalid_json')
        if not isinstance(data, dict):
            raise AssistantError('Body must be a JSON object.', 'invalid_json')
        return data

    def _api_base_url(self):
        url = os.environ.get('APQMS_PUBLIC_URL', '').strip().rstrip('/')
        if url:
            return url
        proto = (self.headers.get('X-Forwarded-Proto') or 'http').split(',')[0].strip()
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'localhost').split(',')[0]
        return f'{proto}://{host.strip()}'

    # ── entry point ──────────────────────────────────────────
    def _api_dispatch(self, method, path, qs, raw_body=b''):
        try:
            return self._api_route(method, path, qs, raw_body)
        except AssistantError as e:
            return self._api_json(e.to_dict(), e.status)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._api_error(500, 'internal_error', 'Unexpected server error (logged).')

    def _api_route(self, method, path, qs, raw):
        def q(key, default=''):
            return (qs.get(key) or [default])[0]

        # Public endpoints
        if path == '/api/v1/health' and method == 'GET':
            return self._api_json({'ok': True, 'service': 'apqms', 'version': 1})
        if path == '/api/v1/openapi.json' and method == 'GET':
            return self._api_json(openapi_spec(self._api_base_url()))
        if path == '/api/v1/telegram/webhook' and method == 'POST':
            if not telegram.check_webhook_secret(self.headers.get('X-Telegram-Bot-Api-Secret-Token')):
                return self._api_error(403, 'forbidden', 'Bad or missing webhook secret.')
            try:
                update = json.loads(raw.decode('utf-8') or '{}')
            except (UnicodeDecodeError, ValueError):
                update = {}
            if isinstance(update, dict):
                telegram.handle_update(update)
            return self._api_json({'ok': True})    # always 200 so Telegram doesn't retry forever

        user = tokens.authenticate(self.headers.get('Authorization'))
        if user is None:
            return self._api_error(401, 'unauthorized',
                                   'Missing or invalid API token (Authorization: Bearer apqms_...).')

        if method == 'GET':
            if path == '/api/v1/me':
                return self._api_json({'user': user})
            if path == '/api/v1/summary':
                return self._api_json(core.summary())
            if path == '/api/v1/customers':
                return self._api_json({'customers': core.search_customers(q('q'), _limit(q('limit')))})
            if path == '/api/v1/items':
                return self._api_json({'items': core.search_items(q('q'), _limit(q('limit')))})
            m = re.fullmatch(r'/api/v1/(quotations|proformas?)', path)
            if m:
                kind = _KIND_SEG[m.group(1)]
                return self._api_json({'documents': core.list_documents(
                    kind, q('q'), q('status'), q('limit', '20'), q('offset', '0'))})
            m = re.fullmatch(r'/api/v1/(quotations|proformas?)/' + _REF + r'(/pdf)?', path)
            if m:
                kind = _KIND_SEG[m.group(1)]
                if m.group(3):
                    pdf, name = core.render_pdf(kind, m.group(2))
                    return self.send(pdf, 200, [
                        ('Content-Type', 'application/pdf'),
                        ('Content-Disposition', f'attachment; filename="{name}"')] + _cors_headers())
                return self._api_json({'document': core.get_document(kind, m.group(2))})
            return self._api_error(404, 'not_found', 'Unknown endpoint.')

        if method != 'POST':
            return self._api_error(405, 'method_not_allowed', 'Use GET or POST.')
        body = self._api_body(raw)

        if path == '/api/v1/chat':
            message = str(body.get('message') or '').strip()
            if not message:
                raise AssistantError('"message" is required.', 'message_required')
            conv = str(body.get('conversation_id') or 'default')[:60]
            want_pdf = bool(body.get('include_pdf_base64'))
            reply = bot.handle(message, user, chat_key=f"api:{user['id']}:{conv}", attach_pdf=want_pdf)
            out = {'ok': reply.ok, 'reply': reply.text}
            out.update(reply.data)
            doc = reply.data.get('document')
            if doc and doc.get('id'):
                out['pdf_url'] = self._api_base_url() + doc['pdf_path']
            if reply.pdf and want_pdf:
                out['pdf_base64'] = base64.b64encode(reply.pdf).decode()
                out['pdf_filename'] = reply.pdf_name
            return self._api_json(out, 200 if reply.ok else 422)
        if path == '/api/v1/customers':
            return self._api_json({'customer': core.create_customer(body)}, 201)
        m = re.fullmatch(r'/api/v1/(quotations|proformas?)', path)
        if m:
            doc = core.create_document(_KIND_SEG[m.group(1)], body, user['id'])
            if doc.get('id'):
                doc['pdf_url'] = self._api_base_url() + doc['pdf_path']
            return self._api_json({'document': doc}, 200 if body.get('preview') else 201)
        m = re.fullmatch(r'/api/v1/(quotations|proformas?)/' + _REF + r'/status', path)
        if m:
            return self._api_json({'document': core.set_status(
                _KIND_SEG[m.group(1)], m.group(2), body.get('status'))})
        m = re.fullmatch(r'/api/v1/quotations/' + _REF + r'/proforma', path)
        if m:
            pi = core.quotation_to_proforma(
                m.group(1), user['id'], order_no=str(body.get('order_no') or '')[:100],
                order_date=str(body.get('order_date') or '')[:30],
                mark_confirmed=bool(body.get('mark_confirmed')))
            pi['pdf_url'] = self._api_base_url() + pi['pdf_path']
            return self._api_json({'document': pi}, 201)
        return self._api_error(404, 'not_found', 'Unknown endpoint.')


def _limit(raw, default=10):
    try:
        return max(1, min(int(raw), 100))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────
#  OpenAPI spec — import into ChatGPT "Actions", n8n, Postman, etc.
# ─────────────────────────────────────────────────────────────
def openapi_spec(base_url):
    ref_param = {'name': 'ref', 'in': 'path', 'required': True, 'schema': {'type': 'string'},
                 'description': 'Numeric id or document number, e.g. AQ-2026-012 / API-2026-004'}
    kind_param = {'name': 'kind', 'in': 'path', 'required': True,
                  'schema': {'type': 'string', 'enum': ['quotations', 'proformas']}}
    obj = {'type': 'object'}
    ok = {'200': {'description': 'OK', 'content': {'application/json': {'schema': obj}}}}
    item = {'type': 'object', 'properties': {
        'item': {'type': 'string', 'description': 'Item text, fuzzy-matched to the catalog'},
        'item_id': {'type': 'integer', 'description': 'Exact catalog item id (optional)'},
        'quantity': {'type': 'number'},
        'unit': {'type': 'string', 'description': 'e.g. Mtr., Pcs., Kg., Nos., Set'},
        'rate': {'type': 'number', 'description': 'Per-unit rate; defaults to catalog rate'},
        'description': {'type': 'string'}, 'specification': {'type': 'string'}},
        'required': ['quantity']}
    doc_body = {'type': 'object', 'properties': {
        'customer': {'type': 'string', 'description': 'Customer name (fuzzy), id or GSTIN'},
        'items': {'type': 'array', 'items': item},
        'subject': {'type': 'string'}, 'notes': {'type': 'string'}, 'terms': {'type': 'string'},
        'status': {'type': 'string', 'enum': core.STATUSES[QUOTATION]},
        'date': {'type': 'string', 'description': 'DD - MM - YYYY, default today'},
        'enquiry_no': {'type': 'string', 'description': 'Quotation only'},
        'order_no': {'type': 'string', 'description': 'Proforma only (customer PO no.)'},
        'order_date': {'type': 'string', 'description': 'Proforma only'},
        'preview': {'type': 'boolean', 'description': 'true = resolve & price, do not save'}},
        'required': ['customer', 'items']}
    return {
        'openapi': '3.1.0',
        'info': {'title': 'APQMS Assistant API', 'version': '1.0.0',
                 'description': 'Create quotations / proforma invoices, look up customers and '
                                'items, change status, download PDFs.'},
        'servers': [{'url': base_url}],
        'components': {'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}}},
        'security': [{'bearer': []}],
        'paths': {
            '/api/v1/chat': {'post': {
                'operationId': 'chat',
                'summary': 'Send a free-text command (same as the Telegram bot)',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['message'], 'properties': {
                        'message': {'type': 'string'},
                        'conversation_id': {'type': 'string'},
                        'include_pdf_base64': {'type': 'boolean'}}}}}},
                'responses': ok}},
            '/api/v1/summary': {'get': {'operationId': 'getSummary', 'summary': 'Counts and values',
                                        'responses': ok}},
            '/api/v1/customers': {
                'get': {'operationId': 'searchCustomers', 'summary': 'Search customers',
                        'parameters': [{'name': 'q', 'in': 'query', 'schema': {'type': 'string'}}],
                        'responses': ok},
                'post': {'operationId': 'createCustomer', 'summary': 'Add a customer',
                         'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                             'type': 'object', 'required': ['company_name'], 'properties': {
                                 k: {'type': 'string'} for k in ('company_name', 'contact_person',
                                                                 'phone', 'email', 'gstin', 'address')}}}}},
                         'responses': ok}},
            '/api/v1/items': {'get': {
                'operationId': 'searchItems', 'summary': 'Search the item catalog (with rates)',
                'parameters': [{'name': 'q', 'in': 'query', 'schema': {'type': 'string'}}],
                'responses': ok}},
            '/api/v1/{kind}': {
                'get': {'operationId': 'listDocuments', 'summary': 'List quotations / proformas',
                        'parameters': [kind_param] + [{'name': n, 'in': 'query', 'schema': {'type': 'string'}}
                                                      for n in ('q', 'status', 'limit', 'offset')],
                        'responses': ok},
                'post': {'operationId': 'createDocument', 'summary': 'Create a quotation / proforma',
                         'parameters': [kind_param],
                         'requestBody': {'required': True,
                                         'content': {'application/json': {'schema': doc_body}}},
                         'responses': ok}},
            '/api/v1/{kind}/{ref}': {'get': {
                'operationId': 'getDocument', 'summary': 'Get one document with items and totals',
                'parameters': [kind_param, ref_param], 'responses': ok}},
            '/api/v1/{kind}/{ref}/pdf': {'get': {
                'operationId': 'getDocumentPdf', 'summary': 'Download the PDF',
                'parameters': [kind_param, ref_param],
                'responses': {'200': {'description': 'PDF', 'content': {'application/pdf': {}}}}}},
            '/api/v1/{kind}/{ref}/status': {'post': {
                'operationId': 'setStatus', 'summary': 'Change status',
                'parameters': [kind_param, ref_param],
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['status'], 'properties': {
                        'status': {'type': 'string', 'enum': core.STATUSES[QUOTATION]}}}}}},
                'responses': ok}},
            '/api/v1/quotations/{ref}/proforma': {'post': {
                'operationId': 'convertToProforma', 'summary': 'Make a proforma from a quotation',
                'parameters': [ref_param],
                'requestBody': {'content': {'application/json': {'schema': {
                    'type': 'object', 'properties': {
                        'order_no': {'type': 'string'}, 'order_date': {'type': 'string'},
                        'mark_confirmed': {'type': 'boolean'}}}}}},
                'responses': ok}},
        },
    }
