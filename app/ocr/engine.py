"""
Text extraction for the Scan & Import module.

Turns an uploaded PDF, Word (.docx) or image file into a uniform list of
`Line` objects (each a row of `Cell`s with page-relative x-positions), which
`app/ocr/parser.py` then reads customer details and line items from.

How each file type is read
--------------------------
* PDF   — every page is checked for an embedded text layer first. A page
          that has one is read directly (exact, instant, no OCR errors).
          A page without one (a scan / photo saved as PDF) is rendered to
          an image and read with Tesseract OCR. Pages are decided one by
          one, so a PDF that mixes both works too. "Force OCR" skips the
          text-layer check for PDFs whose embedded text is wrong.
* DOCX  — body text, tables, headers/footers and text boxes are read
          directly from the document XML (exact). Tesseract is used on
          pictures embedded in the document, which is what a "scanned page
          pasted into Word" file is made of. Normal DOCX files with real
          text never need OCR; forcing it would only make them worse.
* Image — PNG/JPG/TIFF/BMP/WEBP, straight through Tesseract.

Rebuilding table rows
---------------------
Tesseract's own line grouping is unreliable for tables, so this module
asks it only for words and their pixel boxes (`image_to_data`), then
regroups the words into rows by vertical position and splits each row into
cells wherever there is a wide horizontal gap. The same regrouping is used
for PDF text layers, so the parser sees the same shape of data from every
source.

Page segmentation mode 3 (fully automatic) is used deliberately. Mode 6
("single uniform block") looks tempting for tables but was measured to drop
the small numeric cells (quantity, unit, serial number columns) on ruled
tables — exactly the data a quotation needs.

Optional dependencies (see requirements-ocr.txt): pytesseract + Pillow +
the Tesseract program itself, pdfplumber (PDF), python-docx (DOCX). Missing
ones raise OcrUnavailableError; `capabilities()` reports what's available so
the page can explain instead of failing.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from statistics import median
from typing import List, Optional


# ── Limits (all overridable via environment) ─────────────────────────
def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_UPLOAD_BYTES = _env_int('OCR_MAX_UPLOAD_MB', 15) * 1024 * 1024
MAX_PAGES = _env_int('OCR_MAX_PAGES', 20)          # pages read per document
OCR_DPI = _env_int('OCR_DPI', 300)                 # PDF render resolution
PAGE_TIMEOUT_SECONDS = _env_int('OCR_PAGE_TIMEOUT', 120)
REQUESTED_LANG = os.environ.get('OCR_LANG', 'eng')  # e.g. "eng+hin"

MAX_IMAGE_PIXELS = 60_000_000        # refuse decompression-bomb images
MAX_OCR_LONG_SIDE = 4600             # downscale anything bigger than this
TINY_LONG_SIDE = 1000                # only images smaller than this are enlarged
TINY_TARGET_LONG_SIDE = 1400
# Two Tesseract passes (auto layout + sparse text) merged word-by-word.
# Measured on noisy scanned tables: one pass finds ~80-84% of the small
# table cells, the merged pair ~92%. Costs 2x OCR time; set OCR_TWO_PASS=0
# on a slow server to trade recall for speed.
TWO_PASS = os.environ.get('OCR_TWO_PASS', '1') != '0'
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_DOCX_IMAGES = 10
MIN_TEXT_LAYER_WORDS = 8             # fewer words than this -> page is OCR'd
LOW_CONFIDENCE = 60.0
# A gap between two words wider than this many "text heights" starts a new
# table cell. Normal word spacing is ~0.3 of a text height; column gaps in
# real tables are well above 1.
CELL_GAP_FACTOR = 1.0


class OcrError(Exception):
    """A problem with the file itself. The message is safe to show users."""


class OcrUnavailableError(Exception):
    """A required library/program isn't installed on this server."""


# ── Data model ───────────────────────────────────────────────────────
@dataclass
class Cell:
    text: str
    x0: float          # horizontal extent on a 0..1000 page-relative scale
    x1: float


@dataclass
class Line:
    cells: List[Cell]
    conf: Optional[float] = None   # mean OCR confidence 0-100; None = exact text
    page: int = 1
    group: Optional[int] = None    # DOCX table id, so column maps don't leak
    # Vertical extent on the same 0..1000-of-page-width scale as Cell.x0/x1
    # (None for DOCX, which has no geometry). Lets the parser decide whether
    # a stray wrapped-text line belongs to the row above or the row below.
    y0: Optional[float] = None
    y1: Optional[float] = None

    @property
    def text(self):
        """Cells separated by tabs (faithful, used for the stored raw text)."""
        return '\t'.join(c.text for c in self.cells)

    @property
    def plain(self):
        """Cells joined by single spaces (used for field-style searching)."""
        return ' '.join(c.text for c in self.cells if c.text).strip()


@dataclass
class ExtractionResult:
    lines: List[Line]
    page_count: int
    method: str                      # text_layer | ocr | mixed | docx | docx+ocr | image
    avg_confidence: Optional[float]  # over OCR'd words only; None if no OCR ran
    warnings: List[str] = field(default_factory=list)

    @property
    def text(self):
        out, page = [], None
        for ln in self.lines:
            if ln.page != page and self.page_count > 1:
                page = ln.page
                out.append(f'--- page {page} ---')
            out.append(ln.text)
        return '\n'.join(out)


@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: Optional[float] = None


# ── Capability detection ─────────────────────────────────────────────
_WINDOWS_TESSERACT_PATHS = (
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
)
_cap_lock = threading.Lock()
_cap_cache = {'at': 0.0, 'value': None}


def _module_ok(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _find_tesseract_cmd():
    explicit = os.environ.get('TESSERACT_CMD')
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which('tesseract')
    if found:
        return found
    for p in _WINDOWS_TESSERACT_PATHS:      # not on PATH after a default Windows install
        if os.path.isfile(p):
            return p
    return None


def _effective_lang(installed):
    """The requested OCR language(s), limited to ones actually installed."""
    wanted = [l for l in REQUESTED_LANG.split('+') if l.strip()] or ['eng']
    usable = [l for l in wanted if l in installed]
    if usable:
        return '+'.join(usable), [l for l in wanted if l not in installed]
    fallback = 'eng' if 'eng' in installed else (installed[0] if installed else 'eng')
    return fallback, wanted


def capabilities(refresh=False):
    """What this server can do. Cached for a minute (tesseract is a
    subprocess call) — a restart or `refresh=True` re-checks."""
    with _cap_lock:
        if not refresh and _cap_cache['value'] and time.time() - _cap_cache['at'] < 60:
            return _cap_cache['value']

        cap = {'pillow': _module_ok('PIL'), 'pytesseract': _module_ok('pytesseract'),
               'pdf': _module_ok('pdfplumber'), 'docx': _module_ok('docx'),
               'tesseract': False, 'version': '', 'lang': 'eng',
               'missing_langs': [], 'detail': ''}
        if not cap['pytesseract']:
            cap['detail'] = 'The Python package "pytesseract" is not installed.'
        elif not cap['pillow']:
            cap['detail'] = 'The Python package "Pillow" is not installed.'
        else:
            import pytesseract
            cmd = _find_tesseract_cmd()
            if not cmd:
                cap['detail'] = ('The Tesseract OCR program was not found on this server. '
                                 'Install it, or set TESSERACT_CMD to its full path.')
            else:
                pytesseract.pytesseract.tesseract_cmd = cmd
                try:
                    cap['version'] = str(pytesseract.get_tesseract_version())
                    installed = [l for l in pytesseract.get_languages(config='') if l != 'osd']
                    cap['lang'], cap['missing_langs'] = _effective_lang(installed)
                    cap['tesseract'] = True
                except Exception as e:  # noqa: BLE001 — any failure means "not usable"
                    cap['detail'] = f'Tesseract was found but could not be run ({e.__class__.__name__}).'
        _cap_cache.update(at=time.time(), value=cap)
        return cap


def _require_tesseract():
    cap = capabilities()
    if not cap['tesseract']:
        raise OcrUnavailableError(cap['detail'] or 'Tesseract OCR is not available.')
    return cap


# ── File type detection (by content, never by the file's name) ───────
_IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', '.png'), (b'\xff\xd8\xff', '.jpg'),
    (b'II*\x00', '.tif'), (b'MM\x00*', '.tif'), (b'BM', '.bmp'),
)


def sniff_file_type(data):
    """-> (kind, extension) with kind in 'pdf' | 'docx' | 'image', or
    (None, reason) when the content isn't a supported document."""
    head = data[:1024]
    if b'%PDF-' in head:
        return 'pdf', '.pdf'
    if data[:4] == b'PK\x03\x04':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None, 'That file looks damaged.'
        if 'word/document.xml' in names:
            return 'docx', '.docx'
        return None, ('That is a zip-based file but not a Word document '
                      '(Excel/PowerPoint files are not supported here).')
    if data[:4] == b'\xd0\xcf\x11\xe0':
        return None, ('Old-format Word/Excel files (.doc/.xls) are not supported. '
                      'Please re-save the file as .docx or PDF and upload that.')
    for sig, ext in _IMAGE_SIGNATURES:
        if data.startswith(sig):
            return 'image', ext
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image', '.webp'
    return None, 'Unsupported file type. Upload a PDF, a Word .docx file, or an image.'


# ── Words -> table-aware lines ───────────────────────────────────────
_NOISE_ONLY = re.compile(r'^[\s|_\-—–=~.,:;\'"`^\\/()\[\]{}*]+$')


def _words_to_lines(words, page_width, page, group=None):
    """Regroup positioned words into rows (by vertical centre), then split
    each row into cells at wide horizontal gaps."""
    words = [w for w in words if w.text.strip() and not _NOISE_ONLY.match(w.text)]
    if not words or page_width <= 0:
        return []

    page_h = median(max(w.bottom - w.top, 1.0) for w in words)
    words.sort(key=lambda w: (w.top + w.bottom) / 2)

    rows, cur = [], [words[0]]
    for w in words[1:]:
        cy = (w.top + w.bottom) / 2
        row_cy = sum((x.top + x.bottom) / 2 for x in cur) / len(cur)
        row_h = median(x.bottom - x.top for x in cur)
        if abs(cy - row_cy) <= 0.5 * max(row_h, 1.0):
            cur.append(w)
        else:
            rows.append(cur)
            cur = [w]
    rows.append(cur)

    scale = 1000.0 / page_width
    lines = []
    for row in rows:
        row.sort(key=lambda w: w.x0)
        row_h = median(w.bottom - w.top for w in row)
        threshold = CELL_GAP_FACTOR * max(row_h, 0.9 * page_h)
        cells, cur_words = [], [row[0]]
        for prev, w in zip(row, row[1:]):
            if w.x0 - prev.x1 > threshold:
                cells.append(cur_words)
                cur_words = [w]
            else:
                cur_words.append(w)
        cells.append(cur_words)

        confs = [w.conf for w in row if w.conf is not None]
        lines.append(Line(
            cells=[Cell(' '.join(w.text for w in c),
                        c[0].x0 * scale, c[-1].x1 * scale) for c in cells],
            conf=(sum(confs) / len(confs)) if confs else None,
            page=page, group=group,
            y0=min(w.top for w in row) * scale, y1=max(w.bottom for w in row) * scale))
    return lines


# ── Tesseract on one image ───────────────────────────────────────────
def _open_image(data):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise OcrError('That image is too large to process.')
        img.load()
        return img
    except OcrError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OcrError('That image could not be read — it may be corrupt.') from e


def _prepare(img):
    """Grayscale, upright, contrast-stretched, and left at NATIVE resolution.

    Enlarging is deliberately avoided: measured on ruled tables in noisy
    scans, upscaling 1.5x-3x made Tesseract lose nearly all small table
    cells (quantities, units) while *adding* speckle "words" — the word
    count goes up but the real data disappears. Only images too small to
    read at all (tiny screenshots) are enlarged, and only modestly."""
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(img)       # phone photos carry a rotation flag
    except Exception:  # noqa: BLE001
        pass
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        rgba = img.convert('RGBA')               # transparent PNG -> black text on white
        bg = Image.new('RGB', rgba.size, 'white')
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    g = img.convert('L')

    long_side = max(g.size)
    if long_side > MAX_OCR_LONG_SIDE:
        f = MAX_OCR_LONG_SIDE / long_side
        g = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))), Image.LANCZOS)
    elif long_side < TINY_LONG_SIDE:
        f = min(2.0, TINY_TARGET_LONG_SIDE / long_side)
        g = g.resize((int(g.width * f), int(g.height * f)), Image.LANCZOS)
    return ImageOps.autocontrast(g, cutoff=1)


def _tesseract_pass(img, lang, page, psm):
    import pytesseract
    try:
        d = pytesseract.image_to_data(
            img, lang=lang, config=f'--psm {psm}',
            output_type=pytesseract.Output.DICT, timeout=PAGE_TIMEOUT_SECONDS)
    except RuntimeError as e:                    # pytesseract's timeout / process failure
        raise OcrError(f'OCR took too long or failed on page {page}. '
                       'Try a smaller or clearer scan.') from e
    words = []
    for i, text in enumerate(d['text']):
        text = (text or '').strip()
        try:
            conf = float(d['conf'][i])
        except (TypeError, ValueError):
            continue
        if not text or conf < 0 or _NOISE_ONLY.match(text):
            continue
        if conf < 20 and not any(ch.isdigit() for ch in text):
            continue                             # speckle read as letters
        words.append(_Word(text, d['left'][i], d['left'][i] + d['width'][i],
                           d['top'][i], d['top'][i] + d['height'][i], conf))
    return words


def _overlap_ratio(a, b):
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.bottom, b.bottom) - max(a.top, b.top))
    inter = ix * iy
    union = (a.x1 - a.x0) * (a.bottom - a.top) + (b.x1 - b.x0) * (b.bottom - b.top) - inter
    return inter / union if union > 0 else 0.0


def _merge_passes(primary, secondary):
    """Union of two word lists. Where a word from `secondary` overlaps one
    already present, the more confident reading wins (with a margin, so the
    primary is kept on near-ties); words only `secondary` found are added."""
    merged = list(primary)
    for w in secondary:
        hits = [i for i, m in enumerate(merged) if _overlap_ratio(w, m) > 0.3]
        if not hits:
            merged.append(w)
            continue
        best = max(hits, key=lambda i: merged[i].conf or 0)
        if (w.conf or 0) > (merged[best].conf or 0) + 5:
            merged[best] = w
    return merged


def _run_tesseract(img, lang, page):
    words = _tesseract_pass(img, lang, page, 3)          # auto layout analysis
    if TWO_PASS:
        words = _merge_passes(words, _tesseract_pass(img, lang, page, 11))  # sparse text
    avg = (sum(w.conf for w in words) / len(words)) if words else None
    return words, avg


def _rotate_if_sideways(img, lang):
    """Use Tesseract's orientation detection (needs osd.traineddata). Returns
    a corrected image, or None if the page already looks upright / OSD can't
    decide (it refuses on sparse pages, which is normal)."""
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(img, config='--psm 0',
                                       output_type=pytesseract.Output.DICT, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    angle = int(osd.get('rotate', 0) or 0)
    if angle not in (90, 180, 270) or float(osd.get('orientation_conf', 0) or 0) < 1.5:
        return None
    return img.rotate(-angle, expand=True, fillcolor=255)   # tesseract's angle is clockwise


def _ocr_page_image(img, page, lang):
    """OCR one page image -> (lines, avg_confidence, word_count)."""
    g = _prepare(img)
    words, conf = _run_tesseract(g, lang, page)

    if conf is None or conf < LOW_CONFIDENCE:
        # A weak read is often a sideways/upside-down scan — try correcting it
        # and keep whichever read is better.
        rotated = _rotate_if_sideways(g, lang)
        if rotated is not None:
            words2, conf2 = _run_tesseract(rotated, lang, page)
            if conf2 is not None and (conf is None or conf2 > conf):
                g, words, conf = rotated, words2, conf2

    return _words_to_lines(words, g.width, page), conf, len(words)


# ── PDF ──────────────────────────────────────────────────────────────
def _text_layer_usable(words):
    if len(words) < MIN_TEXT_LAYER_WORDS:
        return False
    junk = sum(1 for w in words if '(cid:' in w['text'] or '\ufffd' in w['text'])
    return junk <= 0.2 * len(words)     # broken font encodings show up as (cid:NN)


def _render_dpi(page):
    """Resolution to render a PDF page for OCR. A scanned page is one big
    embedded picture; re-rendering it at any other resolution than its own
    resamples it and (see _prepare) hurts table recall, so use the picture's
    native DPI. Pages with no such picture (vector text being force-OCR'd)
    use OCR_DPI."""
    page_w, page_h = float(page.width), float(page.height)
    dpi = OCR_DPI
    try:
        for im in page.images:
            cover = (abs(im['x1'] - im['x0']) * abs(im['bottom'] - im['top'])) / (page_w * page_h)
            src_w = im['srcsize'][0]
            width_in = abs(im['x1'] - im['x0']) / 72.0
            if cover >= 0.8 and src_w and width_in > 0:
                dpi = int(round(src_w / width_in))
                break
    except (KeyError, TypeError, IndexError, ZeroDivisionError):
        pass
    dpi = max(100, min(dpi, 400))
    longest_in = max(page_w, page_h) / 72.0
    return max(72, min(dpi, int(MAX_OCR_LONG_SIDE / longest_in)))


def _extract_pdf(data, force_ocr):
    import pdfplumber
    lines, warnings, used = [], [], set()
    conf_sum = conf_n = 0.0
    lang = None

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 — pdfminer raises many things for bad/encrypted files
        raise OcrError('That PDF could not be opened — it may be corrupt or password-protected.') from e

    with pdf:
        total = len(pdf.pages)
        if total == 0:
            raise OcrError('That PDF has no pages.')
        if total > MAX_PAGES:
            warnings.append(f'Only the first {MAX_PAGES} of {total} pages were read.')

        for pno, page in enumerate(pdf.pages[:MAX_PAGES], 1):
            if not force_ocr:
                try:
                    tw = page.extract_words(x_tolerance=2, y_tolerance=2)
                except Exception:  # noqa: BLE001
                    tw = []
                if _text_layer_usable(tw):
                    words = [_Word(w['text'], w['x0'], w['x1'], w['top'], w['bottom']) for w in tw]
                    lines += _words_to_lines(words, float(page.width), pno)
                    used.add('text_layer')
                    page.flush_cache()
                    continue

            cap = _require_tesseract()
            lang = cap['lang']
            dpi = _render_dpi(page)
            try:
                img = page.to_image(resolution=dpi).original
            except Exception as e:  # noqa: BLE001
                raise OcrError(f'Page {pno} of the PDF could not be rendered for OCR.') from e
            page_lines, conf, n = _ocr_page_image(img, pno, lang)
            lines += page_lines
            used.add('ocr')
            if conf is not None:
                conf_sum += conf * n
                conf_n += n
            page.flush_cache()

    method = 'mixed' if len(used) == 2 else (next(iter(used)) if used else 'text_layer')
    avg = (conf_sum / conf_n) if conf_n else None
    return lines, min(total, MAX_PAGES), method, avg, warnings


# ── DOCX ─────────────────────────────────────────────────────────────
def _docx_table_lines(table, group):
    """A table row becomes one Line per physical text line inside its cells
    (so a wrapped/multi-line cell looks exactly like a wrapped OCR row).
    Columns get synthetic, evenly spaced x-positions so the parser can align
    rows to the header by position just as it does for scanned tables."""
    out = []
    for row in table.rows:
        cells = []          # (text_lines, col_start, col_span)
        seen, col = set(), 0
        for c in row.cells:
            key = id(c._tc)
            if key in seen:                       # merged cell repeats itself; widen the first
                if cells:
                    t, s, span = cells[-1]
                    cells[-1] = (t, s, span + 1)
                col += 1
                continue
            seen.add(key)
            text_lines = [ln.strip() for ln in c.text.split('\n')]
            cells.append((text_lines, col, 1))
            col += 1
        height = max((len(t) for t, _, _ in cells), default=0)
        for i in range(height):
            out.append(Line(
                cells=[Cell(t[i] if i < len(t) else '', s * 100.0, (s + span) * 100.0)
                       for t, s, span in cells],
                page=1, group=group))
    return [ln for ln in out if ln.plain]


def _extract_docx(data, force_ocr):
    if not _module_ok('docx'):
        raise OcrUnavailableError('The Python package "python-docx" is not installed.')
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise OcrError('That Word file expands to an unreasonable size and was rejected.')
        doc = Document(io.BytesIO(data))
    except OcrError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OcrError('That Word file could not be opened — it may be corrupt or '
                       'password-protected.') from e

    def one_line(text):
        text = (text or '').strip()
        return Line(cells=[Cell(text, 0.0, 1000.0)]) if text else None

    lines, warnings, seen_boxes = [], [], set()

    # Letterhead often lives in the page header/footer or a text box, and for
    # an incoming enquiry that is where the *customer's* name and address are.
    for section in doc.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                ln = one_line(p.text)
                if ln and ln.text not in seen_boxes:
                    seen_boxes.add(ln.text)
                    lines.append(ln)
    for tx in doc.element.body.iter(qn('w:txbxContent')):
        for p in tx.iter(qn('w:p')):
            ln = one_line(''.join(t.text or '' for t in p.iter(qn('w:t'))))
            if ln and ln.text not in seen_boxes:     # AlternateContent stores each box twice
                seen_boxes.add(ln.text)
                lines.append(ln)

    group = 0
    for child in doc.element.body.iterchildren():
        if child.tag == qn('w:p'):
            ln = one_line(Paragraph(child, doc).text)
            if ln:
                lines.append(ln)
        elif child.tag == qn('w:tbl'):
            group += 1
            lines += _docx_table_lines(Table(child, doc), group)

    text_chars = sum(len(l.plain) for l in lines)
    method, avg, page_count = 'docx', None, 1

    # Pictures inside the document: OCR them when the document has almost no
    # real text (a scan pasted into Word) or the user forced OCR.
    if force_ocr or text_chars < 40:
        blobs = [rel.target_part.blob for rel in doc.part.rels.values()
                 if rel.reltype.endswith('/image')]
        if blobs and _require_tesseract():
            lang = capabilities()['lang']
            conf_sum = conf_n = 0.0
            if len(blobs) > MAX_DOCX_IMAGES:
                warnings.append(f'Only the first {MAX_DOCX_IMAGES} of {len(blobs)} '
                                'pictures in the document were read.')
            for blob in blobs[:MAX_DOCX_IMAGES]:
                try:
                    img = _open_image(blob)
                except OcrError:
                    continue
                if min(img.size) < 100:              # logos, bullets, icons
                    continue
                page_count += 1
                page_lines, conf, n = _ocr_page_image(img, page_count, lang)
                lines += page_lines
                if conf is not None:
                    conf_sum += conf * n
                    conf_n += n
                method = 'docx+ocr'
            avg = (conf_sum / conf_n) if conf_n else None

    return lines, page_count, method, avg, warnings


# ── Images ───────────────────────────────────────────────────────────
def _extract_image(data):
    from PIL import ImageSequence
    cap = _require_tesseract()
    img = _open_image(data)
    lines, warnings = [], []
    conf_sum = conf_n = 0.0
    count = 0
    for frame in ImageSequence.Iterator(img):        # multi-page TIFFs
        count += 1
        if count > MAX_PAGES:
            warnings.append(f'Only the first {MAX_PAGES} pages of the image were read.')
            count -= 1
            break
        page_lines, conf, n = _ocr_page_image(frame.copy(), count, cap['lang'])
        lines += page_lines
        if conf is not None:
            conf_sum += conf * n
            conf_n += n
    return lines, count, 'image', ((conf_sum / conf_n) if conf_n else None), warnings


# ── Public entry point ───────────────────────────────────────────────
def extract(data, kind, force_ocr=False):
    """Read a document's text. `kind` is one of 'pdf' | 'docx' | 'image'
    (from sniff_file_type). Raises OcrError (bad file — message is safe to
    show) or OcrUnavailableError (missing library/program)."""
    if not capabilities()['pillow'] and kind in ('image', 'pdf'):
        raise OcrUnavailableError('The Python package "Pillow" is not installed.')

    if kind == 'pdf':
        if not _module_ok('pdfplumber'):
            raise OcrUnavailableError('The Python package "pdfplumber" is not installed.')
        lines, pages, method, avg, warnings = _extract_pdf(data, force_ocr)
    elif kind == 'docx':
        lines, pages, method, avg, warnings = _extract_docx(data, force_ocr)
    elif kind == 'image':
        lines, pages, method, avg, warnings = _extract_image(data)
    else:
        raise OcrError('Unsupported file type.')

    lines = [ln for ln in lines if ln.plain]
    if not lines:
        raise OcrError('No readable text was found in that file. If it is a photo or scan, '
                       'try a sharper, well-lit, upright image.')
    if avg is not None and avg < LOW_CONFIDENCE:
        warnings.append(f'The scan quality is low (OCR confidence {avg:.0f}%). '
                        'Check every figure carefully.')
    return ExtractionResult(lines=lines, page_count=pages, method=method,
                            avg_confidence=avg, warnings=warnings)
