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

MODEL = os.environ.get('APQMS_VISION_MODEL', 'claude-opus-5-5')
EFFORT = os.environ.get('APQMS_VISION_EFFORT', 'high')
MAX_UPLOAD_BYTES = 20 * 1024 * 1024          # what a user may upload
MAX_API_IMAGE_BYTES = 5 * 1024 * 1024        # per-image limit of the API (after base64 decode)
MAX_EDGE_PX = 2400                           # long edge after downscaling
MAX_TEXT_CHARS = 8000
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


def available():
    """(ok, reason) — whether extraction can run on this server."""
    if anthropic is None:
        return False, 'The Anthropic SDK is not installed (pip install -r requirements-vision.txt).'
    if not (os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')
            or os.environ.get('ANTHROPIC_PROFILE')):
        return False, 'ANTHROPIC_API_KEY is not set on the server.'
    return True, ''


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


def _call(client, messages, system=SYSTEM_PROMPT):
    return client.beta.messages.create(
        model=MODEL,
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
        client = anthropic.Anthropic()
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
        client = anthropic.Anthropic()
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
        try:
            response = _call(client, messages, system)
        except Exception as e:
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
                                model=getattr(response, 'model', MODEL) or MODEL,
                                attempts=attempt, usage=usage, preprocessed=changed)
    raise ExtractionError('Extraction failed.', raw_text=last_text)     # not reached


def _api_error_message(e):
    if anthropic is None:
        return 'AI service error.'
    if isinstance(e, anthropic.AuthenticationError):
        return 'The server\'s Anthropic API key is invalid.'
    if isinstance(e, anthropic.PermissionDeniedError):
        return 'The Anthropic API key is not allowed to use this model.'
    if isinstance(e, anthropic.RateLimitError):
        return 'The AI service is busy (rate limit). Please try again in a minute.'
    if isinstance(e, anthropic.BadRequestError):
        return f'The AI service rejected the request: {getattr(e, "message", e)}'
    if isinstance(e, anthropic.APIStatusError):
        return f'AI service error ({e.status_code}). Please try again.'
    if isinstance(e, anthropic.APIConnectionError):
        return 'Could not reach the AI service. Check the server\'s internet connection.'
    return 'Unexpected error while reading the photo.'


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
