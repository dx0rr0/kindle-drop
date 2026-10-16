"""Export PDF images sized and encoded for e-readers: small EPUBs, fast conversion.

Levers, in order of impact:
1. Read each image's original stream once (extract_image) instead of decoding it per page.
2. Grayscale: Kindle screens show 16 gray levels, so color only costs bytes. JPEGs are
   decoded straight to gray and downscaled by the decoder (draft), which is cheap.
3. Downscale to an e-reader screen (1264 px wide covers 300 ppi Kindles).
4. Photographs and screenshots become JPEG; flat line art becomes a gray PNG.
5. Pass gray JPEGs that already fit through byte for byte: no work, no quality loss.
6. Encode in background threads while the next pages are read; Pillow releases the GIL
   while decoding and compressing, so image work overlaps text extraction.
7. Keep results in memory: on Windows, antivirus scanning makes each temporary file costly.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
from io import BytesIO
import os

import pymupdf
from PIL import Image

MAX_SIZE = (1264, 1680)
JPEG_QUALITY = 80
FLAT_LEVELS = 64  # at most this many gray levels: line art, kept lossless as PNG
MAX_PIXELS = 40_000_000
WORKERS = min(8, os.cpu_count() or 2)


def fits(width, height):
    return width <= MAX_SIZE[0] and height <= MAX_SIZE[1]


def gray(image):
    """Composite transparency onto white paper, then convert to 8-bit gray."""
    if image.mode in ('RGBA', 'LA', 'PA') or (image.mode == 'P' and 'transparency' in image.info):
        image = image.convert('RGBA')
        paper = Image.new('RGB', image.size, 'white')
        paper.paste(image, mask=image.getchannel('A'))
        image = paper
    return image if image.mode == 'L' else image.convert('L')


def encode(image):
    """Return (bytes, extension) for a decoded image."""
    image.draft('L', MAX_SIZE)  # JPEG: decode directly to gray at a reduced scale
    image = gray(image)
    image.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    buffer = BytesIO()
    if image.getcolors(FLAT_LEVELS) is not None:  # diagram or icon: lossless and crisp
        image.save(buffer, 'PNG')
        return buffer.getvalue(), 'png'
    image.save(buffer, 'JPEG', quality=JPEG_QUALITY)
    return buffer.getvalue(), 'jpg'


def source(doc, picture):
    """Read one image from the PDF: ready bytes for pass-through, else a decoded image."""
    if not picture.xref:  # inline image without an object: rasterize its area
        pixmap = doc[picture.page].get_pixmap(clip=pymupdf.Rect(*vars(picture.box).values()), dpi=150)
        return None, Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
    raw = doc.extract_image(picture.xref)
    if not raw or raw['width'] * raw['height'] > MAX_PIXELS:
        return None, None
    if raw['ext'] in ('jpeg', 'jpg') and not raw.get('smask') and raw['colorspace'] == 1 and fits(raw['width'], raw['height']):
        return (raw['image'], 'jpg'), None
    if raw['ext'] in ('png', 'jpeg', 'jpg') and not raw.get('smask') and raw['colorspace'] in (1, 3):
        return None, Image.open(BytesIO(raw['image']))
    # CMYK, indexed, JPX, JBIG2 or soft-masked: let MuPDF decode and convert to RGB.
    pixmap = pymupdf.Pixmap(doc, picture.xref)
    if raw.get('smask'):
        pixmap = pymupdf.Pixmap(pixmap, pymupdf.Pixmap(doc, raw['smask']))
    if pixmap.colorspace and pixmap.colorspace.n != 1:
        pixmap = pymupdf.Pixmap(pymupdf.csGRAY, pixmap)
    mode = {1: 'L', 3: 'RGB'}.get(pixmap.n - pixmap.alpha, 'RGB') + ('A' if pixmap.alpha else '')
    return None, Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)


def asset(data, extension):
    return f'images/{hashlib.sha256(data).hexdigest()[:20]}.{extension}', data


class Exporter:
    """Encode each distinct picture once in background threads.

    add() only reads the image stream (PyMuPDF stays on the caller's thread); decoding,
    resizing and compression run in the pool while the caller extracts further pages.
    """

    def __init__(self, doc, workers=WORKERS):
        self.doc, self.assets, self.pending = doc, {}, {}
        self.pool = ThreadPoolExecutor(workers)
        self.in_flight = 8 * workers  # bounds images held in memory; JPEGs decode lazily

    def add(self, picture):
        key = picture.key
        if key in self.assets or key in self.pending:
            return
        try:
            ready, image = source(self.doc, picture)
        except Exception:
            return  # an unreadable image is skipped rather than failing the whole book
        if ready:
            self.assets[key] = asset(*ready)
        elif image is not None:
            self.pending[key] = self.pool.submit(encode, image)
        if len(self.pending) >= self.in_flight:
            done, _ = wait(self.pending.values(), return_when=FIRST_COMPLETED)
            for finished in [k for k, f in self.pending.items() if f in done]:
                self.collect(finished)

    def collect(self, key):
        try:
            self.assets[key] = asset(*self.pending.pop(key).result())
        except Exception:
            pass

    def finish(self):
        """Wait for all encodings; return {picture key: (relative name, bytes)}."""
        for key in list(self.pending):
            self.collect(key)
        self.pool.shutdown()
        return self.assets
