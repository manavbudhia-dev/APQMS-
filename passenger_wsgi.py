"""
passenger_wsgi.py
==================
WSGI adapter for the Aypee Industries Quotation Management System.

server.py was written as a standalone socket server (http.server +
BaseHTTPRequestHandler), NOT a WSGI app. Shared hosting (cPanel "Setup
Python App" / Passenger) can only run WSGI apps, so this file wraps the
existing QuotationHandler routing logic in a WSGI-compatible shim -
WITHOUT modifying server.py itself.

Do not rename/move this file - Passenger looks for it by this exact name
in the application root, and imports the `application` object from it.
"""
import io
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import QuotationHandler, init_db

# Make sure the database/tables exist (server.py normally does this
# inside `if __name__ == '__main__'`, which never runs under Passenger).
init_db()


class WSGIQuotationHandler(QuotationHandler):
    """Same routing/page logic as QuotationHandler, fed by a WSGI environ
    instead of a live socket."""

    def __init__(self, environ):
        # Deliberately DO NOT call super().__init__() - that version
        # expects a real socket connection. We set up just the attributes
        # that do_GET/do_POST/send/serve_static actually touch.
        self.environ = environ
        self.command = environ.get('REQUEST_METHOD', 'GET')

        path = environ.get('PATH_INFO', '/') or '/'
        qs = environ.get('QUERY_STRING', '')
        self.path = f'{path}?{qs}' if qs else path

        self.headers = self._build_headers(environ)
        self.rfile = environ.get('wsgi.input', io.BytesIO())
        self.wfile = io.BytesIO()
        self.client_address = (environ.get('REMOTE_ADDR', ''), 0)

        self._wsgi_status = '200 OK'
        self._wsgi_headers = []

    @staticmethod
    def _build_headers(environ):
        headers = {}
        for key, value in environ.items():
            if key.startswith('HTTP_'):
                name = '-'.join(part.capitalize() for part in key[5:].split('_'))
                headers[name] = value
        if environ.get('CONTENT_TYPE'):
            headers['Content-Type'] = environ['CONTENT_TYPE']
        if environ.get('CONTENT_LENGTH'):
            headers['Content-Length'] = environ['CONTENT_LENGTH']

        class Headers(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)
        h = Headers()
        h.update(headers)
        return h

    # ── Overrides of BaseHTTPRequestHandler's low-level I/O ──
    def send_response(self, code, message=None):
        import http.client
        msg = message or http.client.responses.get(code, '')
        self._wsgi_status = f'{code} {msg}'

    def send_header(self, key, value):
        self._wsgi_headers.append((str(key), str(value)))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass


def application(environ, start_response):
    handler = WSGIQuotationHandler(environ)
    try:
        if handler.command == 'GET':
            handler.do_GET()
        elif handler.command == 'POST':
            handler.do_POST()
        elif handler.command == 'OPTIONS':
            handler.do_OPTIONS()
        else:
            handler.send(b'Method Not Allowed', 405, [('Content-Type', 'text/plain')])
    except Exception:
        # do_GET/do_POST (see app/routing.py) already catch and log
        # anything an individual page handler raises, so reaching this
        # branch means the failure happened outside that — e.g. while
        # building the WSGI environ itself. Either way, a raw traceback
        # must never go to the client: it hands out file paths, request
        # data, and internal structure to whoever triggered the error,
        # authenticated or not. Log it server-side (visible in the
        # Passenger/cPanel error log) and send back a generic message.
        import traceback
        traceback.print_exc(file=sys.stderr)
        err = b'Internal Server Error. Please try again or contact an administrator.'
        handler._wsgi_status = '500 Internal Server Error'
        handler._wsgi_headers = [('Content-Type', 'text/plain; charset=utf-8')]
        handler.wfile = io.BytesIO(err)

    start_response(handler._wsgi_status, handler._wsgi_headers)
    return [handler.wfile.getvalue()]
