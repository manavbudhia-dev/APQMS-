#!/usr/bin/env python3
"""
apqms_cli.py — command-line access to the Quotation Management System.

Works in two modes:
  local   (default)  talks to the database directly. Run on the server.
  remote             talks to the JSON API. Pass --url and --token (or set
                     APQMS_URL / APQMS_TOKEN). Run from anywhere.

Examples
  python3 apqms_cli.py chat "quotation Krishna Sugar: 10 mtr chain 25.4 @ 450"
  python3 apqms_cli.py chat "AQ-2026-012 ka proforma bana do" --save-pdf .
  python3 apqms_cli.py shell                       # interactive, like the bot
  python3 apqms_cli.py create quotation --customer "Krishna Sugar" \\
        --item "roller chain 25.4|10|450|Mtr." --item "bearing 6205|5" --subject "Season spares"
  python3 apqms_cli.py list quotation --status Draft
  python3 apqms_cli.py pdf AQ-2026-012 -o .
  python3 apqms_cli.py convert AQ-2026-012 --order-no PO-4512
  python3 apqms_cli.py token create --user admin --name "my assistant"
  python3 apqms_cli.py telegram poll               # run the Telegram bot without a public URL
  python3 apqms_cli.py telegram set-webhook https://quotes.example.com

  # remote:
  APQMS_URL=https://quotes.example.com APQMS_TOKEN=apqms_xxx python3 apqms_cli.py chat "summary"
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── remote client ────────────────────────────────────────────
class Remote:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip('/'), token

    def call(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json',
            'User-Agent': 'apqms-cli/1'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = r.read()
                return payload if raw else json.loads(payload.decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())
            except Exception:
                err = {'error': f'http_{e.code}', 'message': str(e)}
            if raw:
                raise SystemExit(f"Error: {err.get('message')}")
            return err


def _client(args):
    url = args.url or os.environ.get('APQMS_URL', '')
    if not url:
        return None
    token = args.token or os.environ.get('APQMS_TOKEN', '')
    if not token:
        raise SystemExit('Remote mode needs --token or APQMS_TOKEN.')
    return Remote(url, token)


def _local_user(args):
    from database import init_db
    from app.assistant import core
    init_db()
    u = core.user_by_username(args.user)
    if u is None:
        raise SystemExit(f'User "{args.user}" not found or inactive (use --user).')
    return u


def _save_pdf(content, name, out):
    if not out:
        return
    path = os.path.join(out, name) if os.path.isdir(out) else out
    with open(path, 'wb') as f:
        f.write(content)
    print(f'PDF saved: {path}')


def _print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ── commands ─────────────────────────────────────────────────
def cmd_chat(args, message=None):
    message = message if message is not None else ' '.join(args.message)
    if message == '-':
        message = sys.stdin.read()
    if message.strip() == '' and not sys.stdin.isatty():
        message = sys.stdin.read()
    rc = _client(args)
    if rc:
        res = rc.call('POST', '/api/v1/chat', {'message': message, 'conversation_id': 'cli',
                                               'include_pdf_base64': bool(args.save_pdf)})
        print(res.get('reply') or res.get('message') or res)
        if res.get('pdf_base64'):
            _save_pdf(base64.b64decode(res['pdf_base64']), res.get('pdf_filename', 'document.pdf'),
                      args.save_pdf)
        elif res.get('pdf_url'):
            print(f"PDF: {res['pdf_url']}")
        return 0 if res.get('ok', True) else 1
    from app.assistant import bot
    reply = bot.handle(message, _local_user(args), chat_key=f'cli:{args.user}',
                       attach_pdf=bool(args.save_pdf))
    print(reply.text)
    if reply.pdf:
        _save_pdf(reply.pdf, reply.pdf_name, args.save_pdf)
    return 0 if reply.ok else 1


def cmd_shell(args):
    print('APQMS shell — bot jaisa hi likhein ("help"). Multi-line message: khaali line par Enter '
          'dabane tak likhte rahein. Exit: Ctrl+D / "exit".')
    while True:
        try:
            first = input('apqms> ')
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if first.strip().lower() in ('exit', 'quit'):
            return 0
        lines = [first]
        if not first.strip():
            continue
        # Single-line commands run straight away; anything that looks like the
        # start of a document collects lines until an empty one.
        from app.assistant.parser import _kind_in, _doc_ref
        if _kind_in(first) and not _doc_ref(first) and ':' not in first:
            while True:
                try:
                    nxt = input('  ...> ')
                except EOFError:
                    break
                if not nxt.strip():
                    break
                lines.append(nxt)
        cmd_chat(args, '\n'.join(lines))


def _parse_item_arg(spec):
    parts = [p.strip() for p in spec.split('|')]
    item = {'item': parts[0]}
    if len(parts) > 1 and parts[1]:
        item['quantity'] = parts[1]
    if len(parts) > 2 and parts[2]:
        item['rate'] = parts[2]
    if len(parts) > 3 and parts[3]:
        item['unit'] = parts[3]
    return item


def cmd_create(args):
    body = {'customer': args.customer, 'items': [_parse_item_arg(i) for i in args.item],
            'preview': args.preview}
    for k in ('subject', 'notes', 'status', 'order_no', 'order_date', 'enquiry_no'):
        v = getattr(args, k, None)
        if v:
            body[k] = v
    rc = _client(args)
    seg = 'quotations' if args.kind.startswith('q') else 'proformas'
    if rc:
        res = rc.call('POST', f'/api/v1/{seg}', body)
        if 'error' in res:
            raise SystemExit(f"Error: {res['message']}")
        doc = res['document']
    else:
        from app.assistant import core
        try:
            doc = core.create_document(args.kind, body, _local_user(args)['id'])
        except core.AssistantError as e:
            raise SystemExit(f'Error: {e.message}')
    if args.json:
        _print_json(doc)
    else:
        from app.assistant.bot import format_doc
        print(format_doc(doc))
    if args.save_pdf and doc.get('id'):
        _fetch_pdf(args, seg, doc['id'], args.save_pdf)
    return 0


def _fetch_pdf(args, seg, ref, out):
    rc = _client(args)
    if rc:
        content = rc.call('GET', f'/api/v1/{seg}/{urllib.parse.quote(str(ref))}/pdf', raw=True)
        from app.assistant import core
        name = f'{ref}.pdf'
        if str(ref).isdigit():
            name = rc.call('GET', f'/api/v1/{seg}/{ref}')['document']['number'] + '.pdf'
        _save_pdf(content, name, out)
    else:
        from app.assistant import core
        content, name = core.render_pdf('quotation' if seg == 'quotations' else 'proforma', ref)
        _save_pdf(content, name, out)


def cmd_pdf(args):
    from app.assistant import core
    kind = core.kind_from_number(args.ref) or ('proforma' if args.proforma else 'quotation')
    _fetch_pdf(args, 'quotations' if kind == 'quotation' else 'proformas', args.ref, args.output)
    return 0


def cmd_list(args):
    seg = 'quotations' if args.kind.startswith('q') else 'proformas'
    rc = _client(args)
    if rc:
        q = urllib.parse.urlencode({'q': args.q or '', 'status': args.status or '', 'limit': args.limit})
        rows = rc.call('GET', f'/api/v1/{seg}?{q}').get('documents', [])
    else:
        from app.assistant import core
        _local_user(args)
        rows = core.list_documents(args.kind, args.q, args.status, args.limit)
    if args.json:
        _print_json(rows)
    else:
        for r in rows:
            print(f"{r['number']:<16} {r['date']:<16} {r['status']:<10} {r['grand_total']:>14,.2f}  {r['customer']}")
    return 0


def cmd_convert(args):
    rc = _client(args)
    body = {'order_no': args.order_no or '', 'order_date': args.order_date or '',
            'mark_confirmed': args.confirm}
    if rc:
        res = rc.call('POST', f'/api/v1/quotations/{urllib.parse.quote(args.ref)}/proforma', body)
        if 'error' in res:
            raise SystemExit(f"Error: {res['message']}")
        doc = res['document']
    else:
        from app.assistant import core
        try:
            doc = core.quotation_to_proforma(args.ref, _local_user(args)['id'], body['order_no'],
                                             body['order_date'], args.confirm)
        except core.AssistantError as e:
            raise SystemExit(f'Error: {e.message}')
    from app.assistant.bot import format_doc
    print(format_doc(doc))
    if args.save_pdf:
        _fetch_pdf(args, 'proformas', doc['id'], args.save_pdf)
    return 0


def cmd_token(args):
    from app.assistant import tokens
    if args.action == 'create':
        user = _local_user(args)
        tid, token = tokens.create_token(user['id'], args.name)
        print(f'Token #{tid} for {user["username"]} (save it now, it is not shown again):\n{token}')
    elif args.action == 'list':
        _local_user(args)
        for t in tokens.list_tokens():
            print(f"#{t['id']:<4} {t['username']:<12} {t['name']:<30} {t['token_hint']:<20} "
                  f"{'REVOKED' if t['revoked'] else 'active':<8} last used: {t['last_used_at'] or '-'}")
    elif args.action == 'revoke':
        if args.id is None:
            raise SystemExit('token revoke needs an id (see: token list)')
        print('Revoked.' if tokens.revoke_token(args.id) else 'No such token.')
    return 0


def cmd_telegram(args):
    from app.assistant import telegram
    if not telegram.bot_token():
        raise SystemExit('Set APQMS_TELEGRAM_BOT_TOKEN first (get it from @BotFather).')
    if args.action == 'poll':
        _local_user(argparse.Namespace(user=os.environ.get('APQMS_TELEGRAM_USER', 'admin')))
        try:
            telegram.poll()
        except KeyboardInterrupt:
            print('\nStopped.')
    elif args.action == 'set-webhook':
        if not args.base_url:
            raise SystemExit('Usage: telegram set-webhook https://your-public-site')
        _print_json(telegram.set_webhook(args.base_url))
    elif args.action == 'delete-webhook':
        _print_json(telegram.delete_webhook())
    elif args.action == 'info':
        _print_json(telegram._call('getWebhookInfo'))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='apqms_cli.py', description='APQMS command line',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--url', help='remote mode: server base URL (or APQMS_URL)')
    p.add_argument('--token', help='remote mode: API token (or APQMS_TOKEN)')
    p.add_argument('--user', default=os.environ.get('APQMS_CLI_USER', 'admin'),
                   help='local mode: app username to act as (default admin)')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('chat', help='send a free-text command, same as the bot')
    s.add_argument('message', nargs='*')
    s.add_argument('--save-pdf', metavar='PATH', help='save the PDF (file or folder)')
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser('shell', help='interactive chat')
    s.add_argument('--save-pdf', metavar='DIR')
    s.set_defaults(fn=cmd_shell)

    s = sub.add_parser('create', help='create a quotation / proforma from arguments')
    s.add_argument('kind', choices=['quotation', 'proforma'])
    s.add_argument('--customer', required=True)
    s.add_argument('--item', action='append', required=True,
                   help='"name|qty|rate|unit" (rate & unit optional — catalog fills them)')
    for k in ('subject', 'notes', 'status', 'order-no', 'order-date', 'enquiry-no'):
        s.add_argument('--' + k)
    s.add_argument('--preview', action='store_true', help='price it without saving')
    s.add_argument('--json', action='store_true')
    s.add_argument('--save-pdf', metavar='PATH')
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser('list', help='list recent quotations / proformas')
    s.add_argument('kind', choices=['quotation', 'proforma'])
    s.add_argument('--q', default='')
    s.add_argument('--status', default='')
    s.add_argument('--limit', type=int, default=20)
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('pdf', help='download a PDF')
    s.add_argument('ref', help='AQ-2026-012 / API-2026-004 / numeric id')
    s.add_argument('--proforma', action='store_true', help='numeric id is a proforma')
    s.add_argument('-o', '--output', default='.')
    s.set_defaults(fn=cmd_pdf)

    s = sub.add_parser('convert', help='make a proforma from a quotation')
    s.add_argument('ref')
    s.add_argument('--order-no')
    s.add_argument('--order-date')
    s.add_argument('--confirm', action='store_true', help='also mark the quotation Confirmed')
    s.add_argument('--save-pdf', metavar='PATH')
    s.set_defaults(fn=cmd_convert)

    s = sub.add_parser('token', help='manage API tokens (local mode only)')
    s.add_argument('action', choices=['create', 'list', 'revoke'])
    s.add_argument('id', nargs='?', type=int)
    s.add_argument('--name', default='cli')
    s.set_defaults(fn=cmd_token)

    s = sub.add_parser('telegram', help='run / configure the Telegram bot')
    s.add_argument('action', choices=['poll', 'set-webhook', 'delete-webhook', 'info'])
    s.add_argument('base_url', nargs='?')
    s.set_defaults(fn=cmd_telegram)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == '__main__':
    sys.exit(main())
