"""Read positioned text, images and simple vector marks from a PDF with PyMuPDF."""
from __future__ import annotations

import re

import pymupdf

from .model import Line, Page, Picture, Rect, Span

MAX_PAGES = 2000
MIN_PICTURE = 30  # points; smaller images are icons or decorations
MONO = re.compile(r'mono|courier|typewriter|consol|menlo|inconsolata|code', re.I)
BOLD = re.compile(r'bold|medi|semibold|demi|black|heavy', re.I)
ITALIC = re.compile(r'ital|oblique', re.I)
# Images are located separately (get_image_info) so text extraction never decodes them.
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


def open_pdf(data):
    try:
        doc = pymupdf.open(stream=data, filetype='pdf')
    except Exception as error:
        raise ValueError('The file is not a readable PDF.') from error
    if doc.needs_pass:
        raise ValueError('The PDF is password protected.')
    if doc.page_count > MAX_PAGES:
        raise ValueError(f'The PDF exceeds the {MAX_PAGES}-page limit.')
    return doc


def span(raw):
    font, flags = raw['font'], raw['flags']
    return Span(text=raw['text'], size=raw['size'],
                bold=bool(flags & pymupdf.TEXT_FONT_BOLD) or bool(BOLD.search(font)),
                italic=bool(flags & pymupdf.TEXT_FONT_ITALIC) or bool(ITALIC.search(font)),
                mono=bool(flags & pymupdf.TEXT_FONT_MONOSPACED) or bool(MONO.search(font)),
                sup=bool(flags & pymupdf.TEXT_FONT_SUPERSCRIPT), baseline=raw['origin'][1],
                width=raw['bbox'][2] - raw['bbox'][0])


def mark_scripts(spans):
    """Smaller text raised or lowered against the line's main baseline is a super/subscript."""
    main = max(spans, key=lambda s: (s.size, len(s.text.strip())))
    for s in spans:
        if s.text.strip() and s.size < 0.85 * main.size:
            shift = s.baseline - main.baseline
            s.sup = s.sup or shift < -0.15 * main.size
            s.sub = not s.sup and shift > 0.1 * main.size


def pictures(page):
    """Locate images and their PDF objects without decoding them.

    get_image_info(xrefs=True) hashes every decoded image to find its xref, which is
    slow on illustrated books; matching by pixel size against the page's image list is
    exact unless two images share a size, and only then is the slow path used.
    """
    infos = page.get_image_info()
    by_size = {}
    for item in page.get_images(full=True):
        by_size.setdefault((item[2], item[3]), set()).add(item[0])
    if any(len(by_size.get((i['width'], i['height']), ())) != 1 for i in infos):
        infos = page.get_image_info(xrefs=True)
    found = []
    for info in infos:
        box = Rect(*info['bbox'])
        if box.width >= MIN_PICTURE and box.height >= MIN_PICTURE:
            xref = info.get('xref') or next(iter(by_size.get((info['width'], info['height']), {0})))
            found.append(Picture(page.number, box, xref))
    return found


def marks(page):
    """Small filled shapes are list bullets; tinted rectangles frame asides."""
    bullets, frames = [], []
    area = page.rect.width * page.rect.height
    for drawing in page.get_drawings():
        fill = drawing.get('fill')
        if fill is None:
            continue
        box = Rect(*drawing['rect'])
        if 1 < box.width <= 8 and 1 < box.height <= 8:
            bullets.append(box)
        elif sum(fill) < 2.9 and box.width > 100 and box.height > 20 and box.width * box.height < 0.8 * area:
            frames.append(box)
    return bullets, frames


def continues_row(left, right):
    """True when right is a later fragment of the same printed line.

    Loosely justified lines can leave ~4em word gaps that split a line into fragments.
    Another column never lands here: merge_fragments only looks at the last few lines in
    stream order, and a column ends and the next begins on different rows.
    """
    overlap = min(left.box.y1, right.box.y1) - max(left.box.y0, right.box.y0)
    gap = right.box.x0 - left.box.x1
    return overlap > 0.6 * min(left.box.height, right.box.height) and -1 <= gap < 4 * left.size


def merge_fragments(lines):
    merged = []
    for line in lines:
        target = next((m for m in reversed(merged[-4:]) if continues_row(m, line)), None)
        if target is None:
            merged.append(line)
            continue
        if not target.text.endswith(' ') and not line.text.startswith(' '):
            target.spans.append(Span(' ', target.spans[-1].size))
        target.spans.extend(line.spans)
        target.box = Rect(target.box.x0, min(target.box.y0, line.box.y0), line.box.x1, max(target.box.y1, line.box.y1))
    return merged


def read_page(page):
    number = page.number
    result = Page(number, page.rect.width, page.rect.height)
    result.bullets, result.frames = marks(page)
    result.pictures = pictures(page)
    for block in page.get_text('dict', flags=TEXT_FLAGS)['blocks']:
        for raw in block.get('lines', ()):
            if abs(raw['dir'][1]) > 0.1:
                continue  # rotated margin text
            spans = [span(s) for s in raw['spans'] if s['text']]
            if any(s.text.strip() for s in spans):
                mark_scripts(spans)
                result.lines.append(Line(number, Rect(*raw['bbox']), spans))
    result.lines = merge_fragments(result.lines)
    return result


def read_document(doc):
    return [read_page(page) for page in doc]


def render_cover(doc):
    """Use the first page as cover art (JPEG bytes), the way a print edition would look."""
    return doc[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes('jpeg', jpg_quality=85)
