"""
Photo of a quotation sheet -> validated extraction dict.

    result = extract(image_bytes)          # calls Claude
    result.data                            # schema-shaped dict, validated
    result.data['needs_review']            # [{field, reason}, ...]

Steps: optional preprocessing (auto-rotate from EXIF, light contrast boost,
downscale) -> one Messages API call with the image as a base64 block and a
JSON-schema output format -> strip code fences / parse (one retry on bad
JSON) -> `validate()` (types, required fields, sanity checks), which
never invents values: anything it can't trust becomes null plus a
needs_review entry.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field

from app.vision.prompt import (RETRY_PROMPT, SYSTEM_PROMPT, TEXT_SYSTEM_PROMPT, TEXT_USER_PROMPT,
                               USER_PROMPT)
from app.vision.schema import (ATTACHMENT_NUMERIC, EXTRACTION_SCHEMA, NUMERIC_FIELDS, REQUIRED_FIELDS,
                               TEXT_FIELDS, empty_extraction)

try:
    import anthropic
except ImportError:          # optional dependency
    anthropic = None

try:
    from PIL import Image, ImageOps
except ImportError:          # preprocessing is skipped without Pillow
    Image = ImageOps = None

EFFORT = os.environ.get('APQMS_VISION_EFFORT', 'high')
DEFAULT_MODEL = 'claude-opus-5-5'
COMPAT_MAX_TOKENS = 8000          # some gateway / older models cap output lower than 16000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024          # what a user may upload
MAX_API_IMAGE_BYTES = 5 * 1024 * 1024        # per-image limit of the API (after base64 decode)
MAX_EDGE_PX = 2400                           # long edge after downscaling
MAX_TEXT_CHARS = 8000
COMPAT_JSON_INSTRUCTION = ('\n\nOutput format: reply with ONLY one JSON object (no markdown, no code fences, '
                           'no other text) that matches this JSON schema exactly:\n'
                           + json.dumps(EXTRACTION_SCHEMA))
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

_MAGIC = [(b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
          (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif')]


class ExtractionError(Exception):
    """Extraction failed; `message` is safe to show the user."""

    def __init__(self, message, raw_text=''):
        super().__init__(message)
        self.message = message
        self.raw_text = raw_text


@dataclass
class ExtractionResult:
    data: dict
    raw_text: str
    model: str
    attempts: int
    usage: dict = field(default_factory=dict)
    preprocessed: bool = False


# ── AI provider settings (Anthropic directly, or a third-party gateway) ──
#   APQMS_AI_BASE_URL  gateway URL that speaks the Anthropic Messages API
#                      (requests go to <base>/v1/messages). Empty = Anthropic.
#   APQMS_AI_API_KEY   key for that gateway (or Anthropic). Falls back to
#                      ANTHROPIC_API_KEY.
#   APQMS_AI_MODEL     model name as the gateway spells it. Default claude-opus-5-5.
#   APQMS_AI_COMPAT    auto (default) | on | off — "on" sends only the basic
#                      Messages API fields; "auto" does that after the
#                      gateway rejects the full request once.
#   APQMS_AI_PROTOCOL  auto (default) | anthropic | openai — "openai" sends
#                      requests to <base>/v1/chat/completions (OpenAI-style
#                      gateways such as OpenRouter / airouter); "auto" tries
#                      the Anthropic format first and switches to OpenAI
#                      format when the gateway answers 404 for /v1/messages.
def ai_settings():
    base = (os.environ.get('APQMS_AI_BASE_URL') or '').strip().rstrip('/')
    if base.endswith('/v1'):                 # the SDK adds /v1/messages itself
        base = base[:-3]
    key = (os.environ.get('APQMS_AI_API_KEY') or os.environ.get('ANTHROPIC_API_KEY') or '').strip()
    model = (os.environ.get('APQMS_AI_MODEL') or os.environ.get('APQMS_VISION_MODEL') or DEFAULT_MODEL).strip()
    compat = (os.environ.get('APQMS_AI_COMPAT') or 'auto').strip().lower()
    if compat not in ('auto', 'on', 'off'):
        compat = 'auto'
    protocol = (os.environ.get('APQMS_AI_PROTOCOL') or 'auto').strip().lower()
    if protocol not in ('auto', 'anthropic', 'openai'):
        protocol = 'auto'
    if not base:                             # Anthropic direct only speaks its own format
        protocol = 'anthropic'
    try:
        timeout = float(os.environ.get('APQMS_AI_TIMEOUT') or 120)
    except ValueError:
        timeout = 120.0
    return {'base_url': base, 'api_key': key, 'model': model, 'compat': compat, 'timeout': timeout,
            'protocol': protocol}


def available():
    """(ok, reason) — whether extraction can run on this server."""
    st = ai_settings()
    if anthropic is None and st['protocol'] != 'openai':
        return False, 'The Anthropic SDK is not installed (pip install -r requirements-vision.txt).'
    if not (st['api_key'] or os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_PROFILE')):
        return False, 'AI key is not set (APQMS_AI_API_KEY or ANTHROPIC_API_KEY in .env).'
    return True, ''


def make_client():
    """Anthropic SDK client pointed at Anthropic or at the configured gateway.
    For a gateway the key goes in both x-api-key and Authorization: Bearer
    (gateways expect one or the other) with the widely supported
    anthropic-version header."""
    st = ai_settings()
    if _use_openai():
        return OpenAICompatClient()
    kwargs = {'timeout': st['timeout'], 'max_retries': 1}
    if st['base_url']:
        kwargs['base_url'] = st['base_url']
        kwargs['default_headers'] = {'x-api-key': st['api_key'], 'Authorization': f"Bearer {st['api_key']}",
                                     'anthropic-version': '2023-06-01'}
    if st['api_key']:
        kwargs['api_key'] = st['api_key']
    return anthropic.Anthropic(**kwargs)


# Remembered per process: True once a gateway rejected the full request.
_compat_learned = False


# Remembered per process: True once the gateway answered 404 for /v1/messages.
_openai_learned = False


def _use_openai():
    st = ai_settings()
    if st['protocol'] == 'openai':
        return True
    if st['protocol'] == 'anthropic':
        return False
    return _openai_learned


# ── OpenAI-compatible gateways (/v1/chat/completions) ────────
class GatewayHTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(f'{status}: {message}')
        self.status = status
        self.message = message


@dataclass
class _TextBlock:
    text: str
    type: str = 'text'


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Response:
    content: list
    stop_reason: str
    model: str
    usage: _Usage


_FINISH_MAP = {'stop': 'end_turn', 'length': 'max_tokens', 'content_filter': 'refusal'}


def _to_openai_content(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        btype = b.get('type') if isinstance(b, dict) else getattr(b, 'type', '')
        if btype == 'text':
            parts.append({'type': 'text', 'text': b['text'] if isinstance(b, dict) else b.text})
        elif btype == 'image' and isinstance(b, dict):
            src = b['source']
            parts.append({'type': 'image_url',
                          'image_url': {'url': f"data:{src['media_type']};base64,{src['data']}"}})
    if all(p['type'] == 'text' for p in parts):
        return '\n'.join(p['text'] for p in parts)
    return parts


class _OpenAIMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, model, max_tokens, messages, system=None, **_ignored):
        msgs = []
        if system:
            msgs.append({'role': 'system', 'content': system if isinstance(system, str)
                         else ''.join(b.get('text', '') for b in system)})
        for m in messages:
            msgs.append({'role': m['role'], 'content': _to_openai_content(m['content'])})
        payload = self.owner.post('/v1/chat/completions',
                                  {'model': model, 'max_tokens': max_tokens, 'messages': msgs})
        choice = (payload.get('choices') or [{}])[0]
        msg = choice.get('message') or {}
        text = msg.get('content') or ''
        if isinstance(text, list):
            text = ''.join(p.get('text', '') for p in text if isinstance(p, dict))
        if msg.get('refusal') and not text:
            stop = 'refusal'
        else:
            stop = _FINISH_MAP.get(choice.get('finish_reason') or 'stop', 'end_turn')
        u = payload.get('usage') or {}
        return _Response(content=[_TextBlock(text)], stop_reason=stop, model=payload.get('model') or model,
                         usage=_Usage(u.get('prompt_tokens') or 0, u.get('completion_tokens') or 0))


class OpenAICompatClient:
    """Minimal client with the `messages.create(...)` shape the extractor
    uses, speaking the OpenAI Chat Completions format. Standard library
    only; honours HTTPS_PROXY like any urllib request."""

    def __init__(self, base_url=None, api_key=None, timeout=None):
        st = ai_settings()
        self.base_url = (base_url or st['base_url']).rstrip('/')
        self.api_key = api_key if api_key is not None else st['api_key']
        self.timeout = timeout or st['timeout']
        self.messages = _OpenAIMessages(self)
        self.beta = self

    def post(self, path, body):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': f'Bearer {self.api_key}',
                                              'User-Agent': 'apqms-photo-quote/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            try:
                j = json.loads(raw)
                err = j.get('error')
                detail = (err.get('message') if isinstance(err, dict) else None) or j.get('message') or raw
            except ValueError:
                detail = raw
            raise GatewayHTTPError(e.code, str(detail)[:300]) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GatewayHTTPError(0, str(getattr(e, 'reason', e))) from None


def _use_compat():
    st = ai_settings()
    if _use_openai():
        return True
    if st['compat'] == 'on':
        return True
    if st['compat'] == 'off':
        return False
    return _compat_learned


# ── image handling ───────────────────────────────────────────
def sniff_media_type(data):
    for magic, mt in _MAGIC:
        if data.startswith(magic):
            return mt
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def preprocess(data):
    """-> (bytes, media_type, changed). Auto-rotate (phones store rotation
    in EXIF), gentle autocontrast for shadowed photos, downscale very large
    images. No cropping: a wrong crop could cut off a handwritten edit, and
    the prompt already tells the model to ignore the edges."""
    media_type = sniff_media_type(data)
    if Image is None:
        if media_type is None:
            raise ExtractionError('Unsupported image type. Please upload a JPG or PNG photo.')
        if len(data) > MAX_API_IMAGE_BYTES:
            raise ExtractionError('Photo is larger than 5 MB. Install Pillow on the server so it can be '
                                  'resized, or upload a smaller photo.')
        return data, media_type, False
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ExtractionError('Could not open the photo. Please upload a JPG or PNG image '
                              '(iPhone HEIC photos: share as JPG).')
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    img = ImageOps.autocontrast(img, cutoff=1)
    if max(img.size) > MAX_EDGE_PX:
        img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.LANCZOS)
    for quality in (90, 80, 70):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_API_IMAGE_BYTES:
            return out, 'image/jpeg', True
    raise ExtractionError('Photo is too large even after compression.')


# ── parsing & validation ─────────────────────────────────────
_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*(.*?)\s*```\s*$', re.S | re.I)


def parse_json_text(text):
    """Parse model output, tolerating markdown fences / stray prose."""
    t = (text or '').strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        start, end = t.find('{'), t.rfind('}')
        if start != -1 and end > start:
            return json.loads(t[start:end + 1])
        raise


def _to_number(v):
    """-> (number or None, was_coerced)"""
    if v is None:
        return None, False
    if isinstance(v, bool):
        return None, True
    if isinstance(v, (int, float)):
        return (int(v) if float(v).is_integer() else float(v)), False
    if isinstance(v, str):
        s = re.sub(r'(?i)rs\.?|inr|₹|/-|mm|mtrs?|weeks?|%', '', v).replace(',', '').strip()
        try:
            n = float(s)
        except ValueError:
            return None, True
        return (int(n) if n.is_integer() else n), True
    return None, True


def validate(raw):
    """Schema-shaped, type-checked copy of `raw`, plus the list of blocking
    problems (missing required fields). Never guesses: an unusable value
    becomes None and is added to needs_review."""
    data = empty_extraction()
    review = []

    def flag(fieldname, reason):
        review.append({'field': fieldname, 'reason': reason})

    if not isinstance(raw, dict):
        raw = {}
    for f in TEXT_FIELDS:
        v = raw.get(f)
        if v is None:
            continue
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            data[f] = str(v).strip() or None
        else:
            flag(f, 'unexpected value type')
    for f in NUMERIC_FIELDS:
        n, coerced = _to_number(raw.get(f))
        data[f] = n
        if coerced:
            flag(f, f'model returned "{raw.get(f)}" instead of a number'
                    + (f' (read as {n})' if n is not None else ''))
    att = raw.get('attachment') if isinstance(raw.get('attachment'), dict) else {}
    if att.get('material') is not None:
        data['attachment']['material'] = str(att['material']).strip() or None
    for f in ATTACHMENT_NUMERIC:
        n, coerced = _to_number(att.get(f))
        data['attachment'][f] = n
        if coerced:
            flag(f'attachment.{f}', f'model returned "{att.get(f)}" instead of a number')

    for item in raw.get('needs_review') or []:
        if isinstance(item, dict) and item.get('field'):
            flag(str(item['field']).strip(), str(item.get('reason') or 'unclear').strip())
        elif isinstance(item, str) and item.strip():
            flag(item.strip(), 'unclear')

    problems = []
    for f in REQUIRED_FIELDS:
        if data[f] in (None, ''):
            problems.append(f)
            flag(f, 'missing — required')

    # Sanity checks: flag, never "fix".
    if data['rate_per_mtr'] is not None and data['rate_per_mtr'] <= 0:
        flag('rate_per_mtr', 'rate must be positive')
    if data['quantity_mtrs'] is not None and data['quantity_mtrs'] <= 0:
        flag('quantity_mtrs', 'quantity must be positive')
    if data['gst_percent'] is not None and data['gst_percent'] not in (0, 5, 12, 18, 28):
        flag('gst_percent', f'unusual GST rate {data["gst_percent"]}%')
    if data['delivery_weeks'] is not None and not (0 < data['delivery_weeks'] <= 52):
        flag('delivery_weeks', 'delivery period looks wrong')
    if (data['rate_per_mtr'] and data['original_rate_per_mtr']
            and data['rate_per_mtr'] == data['original_rate_per_mtr']):
        flag('original_rate_per_mtr', 'same as the current rate — was the rate really changed?')

    seen, uniq = set(), []
    for r in review:
        key = (r['field'], r['reason'])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    data['needs_review'] = uniq
    return data, problems


# ── the API call ─────────────────────────────────────────────
def _text_of(response):
    return ''.join(b.text for b in response.content if getattr(b, 'type', '') == 'text')


def _call(client, messages, system=SYSTEM_PROMPT, compat=False):
    model = ai_settings()['model']
    if compat or isinstance(client, OpenAICompatClient):
        # Only the basic Messages API fields, which every Anthropic-compatible
        # gateway accepts; the schema moves into the prompt, and parsing /
        # validation / the one retry catch any formatting slip.
        return client.messages.create(
            model=model,
            max_tokens=COMPAT_MAX_TOKENS,
            system=system + COMPAT_JSON_INSTRUCTION,
            messages=messages,
        )
    return client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks='default',          # a safety decline re-runs on Anthropic's recommended model
        system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
        output_config={'effort': EFFORT,
                       'format': {'type': 'json_schema', 'schema': EXTRACTION_SCHEMA}},
        messages=messages,
    )


def extract(image_bytes, client=None, do_preprocess=True):
    """Run the extraction. Raises ExtractionError with a user-facing message.

    Sampling note: the brief asks for temperature 0, but current Opus models
    reject sampling parameters (HTTP 400). Determinism comes instead from
    the JSON-schema output format plus validation and human confirmation."""
    if client is None:
        ok, reason = available()
        if not ok:
            raise ExtractionError(reason)
        client = make_client()
    if not image_bytes:
        raise ExtractionError('No photo received.')
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise ExtractionError(f'Photo is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.')
    if do_preprocess:
        img, media_type, changed = preprocess(image_bytes)
    else:
        img, media_type, changed = image_bytes, sniff_media_type(image_bytes), False
        if media_type is None:
            raise ExtractionError('Unsupported image type. Please upload a JPG or PNG photo.')

    messages = [{'role': 'user', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type,
                                     'data': base64.standard_b64encode(img).decode('ascii')}},
        {'type': 'text', 'text': USER_PROMPT},
    ]}]
    return _run(client, messages, SYSTEM_PROMPT, changed)


def extract_text(text, client=None):
    """Same extraction from text the user copied off the sheet with their
    phone (Google Lens etc.) instead of from the photo. Strikethroughs are
    lost in copied text, so the prompt tells the model to flag every field
    where an old and a new value both appear."""
    if client is None:
        ok, reason = available()
        if not ok:
            raise ExtractionError(reason)
        client = make_client()
    text = (text or '').strip()
    if len(text) < 20:
        raise ExtractionError('Text bahut chhota hai — poori sheet ka text paste karein.')
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f'Text bahut lamba hai (max {MAX_TEXT_CHARS} characters).')
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': TEXT_USER_PROMPT + '\n\n<copied_text>\n' + text + '\n</copied_text>'},
    ]}]
    return _run(client, messages, TEXT_SYSTEM_PROMPT, False)


def _run(client, messages, system, changed):
    usage = {'input_tokens': 0, 'output_tokens': 0}
    last_text, last_err = '', ''
    for attempt in (1, 2):
        global _compat_learned, _openai_learned
        compat = _use_compat() or isinstance(client, OpenAICompatClient)
        try:
            response = _call(client, messages, system, compat)
        except Exception as e:
            st = ai_settings()
            if (st['protocol'] == 'auto' and anthropic is not None and not isinstance(client, OpenAICompatClient)
                    and isinstance(e, anthropic.NotFoundError) and '/v1/messages' in str(e)):
                # The gateway has no Anthropic endpoint: speak OpenAI format.
                _openai_learned = True
                client = OpenAICompatClient()
                try:
                    response = _call(client, messages, system, True)
                except Exception as e2:
                    raise ExtractionError(_api_error_message(e2)) from e2
            elif (not compat and ai_settings()['compat'] == 'auto' and anthropic is not None
                    and isinstance(e, (anthropic.BadRequestError, anthropic.UnprocessableEntityError))):
                # The gateway didn't accept a newer field (fallbacks, betas,
                # output_config, cache_control...). Send the basic request.
                _compat_learned = True
                try:
                    response = _call(client, messages, system, True)
                except Exception as e2:
                    raise ExtractionError(_api_error_message(e2)) from e2
            else:
                raise ExtractionError(_api_error_message(e)) from e
        u = getattr(response, 'usage', None)
        if u is not None:
            usage['input_tokens'] += getattr(u, 'input_tokens', 0) or 0
            usage['output_tokens'] += getattr(u, 'output_tokens', 0) or 0
        if response.stop_reason == 'refusal':
            raise ExtractionError('The AI model declined to read this sheet. Please enter the values '
                                  'manually or try again.')
        if response.stop_reason == 'max_tokens':
            last_err = 'output was cut off'
        last_text = _text_of(response)
        try:
            parsed = parse_json_text(last_text)
            if not isinstance(parsed, dict):
                raise ValueError('not a JSON object')
        except ValueError as e:
            last_err = last_err or str(e)
            if attempt == 1:
                messages = messages + [
                    {'role': 'assistant', 'content': response.content},   # passed back unchanged
                    {'role': 'user', 'content': RETRY_PROMPT.format(error=last_err[:200])},
                ]
                continue
            raise ExtractionError('The AI model did not return valid data twice in a row. '
                                  'Please try again or enter the values manually.', raw_text=last_text)
        data, _problems = validate(parsed)
        return ExtractionResult(data=data, raw_text=last_text,
                                model=getattr(response, 'model', None) or ai_settings()['model'],
                                attempts=attempt, usage=usage, preprocessed=changed)
    raise ExtractionError('Extraction failed.', raw_text=last_text)     # not reached


def _api_error_message(e):
    if isinstance(e, GatewayHTTPError):
        gw = ' (gateway, OpenAI format)'
        d, code = e.message, e.status
        if code == 0:
            return (f'AI service tak connection nahi bana ({ai_settings()["base_url"]}): {d}. '
                    'URL check karein, ya hosting outgoing HTTPS block kar rahi hai.')
        if code == 401:
            return f'AI key galat hai ya expire ho gayi{gw}: {d}'
        if code == 402:
            return f'Gateway account mein balance / credit nahi{gw}: {d}'
        if code == 403:
            return f'Is key ko ye model use karne ki permission nahi{gw}: {d}'
        if code == 404:
            return (f'AI service ne 404 diya{gw} — base URL ya model ka naam galat hai '
                    f'(model: {ai_settings()["model"]}): {d}')
        if code == 429:
            return f'AI service busy hai / limit poori{gw}. Ek minute baad try karein. {d}'
        return f'AI service error {code}{gw}: {d}'
    if anthropic is None:
        return 'AI service error.'
    detail = str(getattr(e, 'message', '') or e)[:300]
    gw = ' (gateway)' if ai_settings()['base_url'] else ''
    if isinstance(e, anthropic.AuthenticationError):
        return f'AI key galat hai ya expire ho gayi{gw}: {detail}'
    if isinstance(e, anthropic.PermissionDeniedError):
        return f'Is key ko ye model use karne ki permission nahi{gw}: {detail}'
    if isinstance(e, anthropic.NotFoundError):
        return (f'AI service ne 404 diya{gw} — base URL ya model ka naam galat hai '
                f'(model: {ai_settings()["model"]}): {detail}')
    if isinstance(e, anthropic.RateLimitError):
        return f'AI service busy hai / limit poori{gw}. Ek minute baad try karein. {detail}'
    if isinstance(e, anthropic.BadRequestError):
        return f'AI service ne request reject ki{gw}: {detail}'
    if isinstance(e, anthropic.APIStatusError):
        return f'AI service error {e.status_code}{gw}: {detail}'
    if isinstance(e, anthropic.APITimeoutError):
        return f'AI service ne time par jawab nahi diya{gw}. Dobara try karein.'
    if isinstance(e, anthropic.APIConnectionError):
        where = ai_settings()['base_url'] or 'api.anthropic.com'
        return f'AI service tak connection nahi bana ({where}). URL check karein, ya hosting outgoing HTTPS block kar rahi hai.'
    return f'Unexpected error while reading the sheet: {detail}'


SAMPLE_SHEET_TEXT = """Forged Type Chain Pitch 142mm (Heavy Duty)
Link forged from 20MnCr5 Hardened & Tempered
Pin (20MnCr5) 25mm with lock washer both sides
Attachment (MOC-EN8D) Height 98mm Thickness 10mm
Total Width 471mm
Quantity: 70 mtrs
Rate: Rs. 7600/- per mtr 83601-bent.
GST 18% extra  Rates ex Factory Jalandhar
Delivery: 5-6 8 weeks from date of order
Payment: 100% on delivery 20 25% advance & balance as used in one month"""


def test_connection(client=None):
    """One real extraction of a small built-in sample. -> dict for display.
    Raises ExtractionError with a readable reason."""
    st = ai_settings()
    res = extract_text(SAMPLE_SHEET_TEXT, client=client)
    return {'ok': True, 'base_url': st['base_url'] or 'https://api.anthropic.com (direct)',
            'model_requested': st['model'], 'model_reported': res.model,
            'mode': ('OpenAI format' if _use_openai() else
                     'compatibility (basic request)' if _use_compat() else 'full'),
            'rate_per_mtr': res.data.get('rate_per_mtr'), 'quantity_mtrs': res.data.get('quantity_mtrs'),
            'delivery_weeks': res.data.get('delivery_weeks'),
            'needs_review': len(res.data.get('needs_review') or []),
            'tokens': res.usage}


def compare(extracted, truth):
    """Field-by-field comparison for evals: -> list of (field, expected, got, ok)."""
    rows = []

    def norm(v):
        return re.sub(r'[\s,.;&]+', ' ', str(v).lower()).strip() if isinstance(v, str) else v

    for key, exp in truth.items():
        if key == 'needs_review':
            continue
        if isinstance(exp, dict):
            for k2, e2 in exp.items():
                got = (extracted.get(key) or {}).get(k2)
                rows.append((f'{key}.{k2}', e2, got, norm(got) == norm(e2)))
        else:
            got = extracted.get(key)
            rows.append((key, exp, got, norm(got) == norm(exp)))
    return rows
