import http.cookiejar, re, urllib.request, urllib.parse, uuid
from html.parser import HTMLParser
BASE = 'http://127.0.0.1:5000'   # overridden per test run: Client(base=...)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

class Client:
    def __init__(self, base=None):
        self.base = base or BASE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
    def req(self, method, path, data=None, headers=None):
        h = {'Origin': self.base}; h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            resp = self.op.open(r, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, dict(resp.headers), resp.read()
    def get(self, path): 
        s,h,b = self.req('GET', path); return s,h,b.decode('utf-8','replace') if 'text' in h.get('Content-Type','') else b
    def post_form(self, path, pairs):
        return self.req('POST', path, urllib.parse.urlencode(pairs).encode(), {'Content-Type':'application/x-www-form-urlencoded'})
    def post_multipart(self, path, fields, files):
        b = uuid.uuid4().hex; body = b''
        for k,v in fields.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k,(fn,ct,data) in files.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b'\r\n'
        body += f'--{b}--\r\n'.encode()
        return self.req('POST', path, body, {'Content-Type': f'multipart/form-data; boundary={b}'})
    def login(self, user, pw):
        s,h,html = self.get('/login'); tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        s,h,b = self.post_form('/login', [('csrf_token',tok),('username',user),('password',pw)]); return s, h.get('Location')
    def csrf_from(self, html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

class FormScraper(HTMLParser):
    """Collect what a browser would submit for the form with the given id."""
    def __init__(self, form_id): super().__init__(); self.fid=form_id; self.inform=False; self.pairs=[]; self.sel=None; self.ta=None; self.intpl=0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag=='template': self.intpl+=1
        if tag=='form': self.inform = (a.get('id')==self.fid)
        if not self.inform or self.intpl: return
        if tag=='input' and a.get('name') and a.get('type') not in ('checkbox','submit','button'): self.pairs.append([a['name'], a.get('value','')])
        elif tag=='select': self.sel=[a['name'], '', None]
        elif tag=='option' and self.sel is not None:
            if 'selected' in a or self.sel[2] is None and False: self.sel[1]=a.get('value','')
        elif tag=='textarea': self.ta=[a['name'], '']
    def handle_endtag(self, tag):
        if tag=='template': self.intpl-=1
        if tag=='form': self.inform=False
        if tag=='select' and self.sel: self.pairs.append(self.sel[:2]); self.sel=None
        if tag=='textarea' and self.ta: self.pairs.append(self.ta); self.ta=None
    def handle_data(self, d):
        if self.ta is not None: self.ta[1] += d
def scrape(html, form_id='ocrForm'):
    p = FormScraper(form_id); p.feed(html); return [tuple(x) for x in p.pairs]
