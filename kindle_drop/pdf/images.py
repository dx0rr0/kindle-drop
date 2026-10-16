"""Export PDF images sized and encoded for e-readers: small EPUBs, fast conversion.

Levers, in order of impact:
1. Read each image's original stream once (extract_image) instead of decoding it per page.
2. Pass JPEGs that already fit the screen through byte for byte: no work, no quality loss.
3. Downscale to an e-reader screen (1264 px wide covers 300 ppi Kindles).
4. Photographs and screenshots become JPEG; flat line art becomes a palette PNG.
5. Encode in parallel threads; Pillow releases the GIL while resizing and compressing.
6. Keep results in memory: on Windows, antivirus scanning makes each temporary file costly.
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
PALETTE_COLORS = 256
MAX_PIXELS = 40_000_000
WORKERS = min(8, os.cpu_count() or 2)
IN_FLIGHT = 2 * WORKERS  # bounds decoded images held in memory


def fits(width, height):
    return width <= MAX_SIZE[0] and height <= MAX_SIZE[1]


def flatten(image):
    """E-readers show transparency badly; composite onto white paper."""
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        image = image.convert('RGBA')
        paper = Image.new('RGB', image.size, 'white')
        paper.paste(image, mask=image.getchannel('A'))
        return paper
    if image.mode not in ('RGB', 'L'):
        return image.convert('RGB')
    return image


def encode(image):
    """Return (bytes, extension) for a decoded image."""
    image.draft(image.mode, MAX_SIZE)  # JPEG decoders can downscale cheaply while decoding
    image = flatten(image)
    image.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    buffer = BytesIO()
    colors = image.getcolors(PALETTE_COLORS)
    if colors is not None:  # flat diagram or icon: exact palette, crisp and small
        image.convert('P', palette=Image.Palette.ADAPTIVE, colors=len(colors)).save(buffer, 'PNG')
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
    if raw['ext'] in ('jpeg', 'jpg') and not raw.get('smask') and raw['colorspace'] in (1, 3) and fits(raw['width'], raw['height']):
        return (raw['image'], 'jpg'), None
    if raw['ext'] in ('png', 'jpeg', 'jpg') and not raw.get('smask') and raw['colorspace'] in (1, 3):
        return None, Image.open(BytesIO(raw['image']))
    # CMYK, indexed, JPX, JBIG2 or soft-masked: let MuPDF decode and convert to RGB.
    pixmap = pymupdf.Pixmap(doc, picture.xref)
    if raw.get('smask'):
        pixmap = pymupdf.Pixmap(pixmap, pymupdf.Pixmap(doc, raw['smask']))
    if pixmap.colorspace and pixmap.colorspace.n not in (1, 3):
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    mode = {1: 'L', 3: 'RGB'}.get(pixmap.n - pixmap.alpha, 'RGB') + ('A' if pixmap.alpha else '')
    return None, Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)


def asset(data, extension):
    return f'images/{hashlib.sha256(data).hexdigest()[:20]}.{extension}', data


def export(doc, pictures):
    """Encode every distinct picture once; return {picture key: (relative name, bytes)}."""
    assets, pending = {}, {}

    def finish(key, future):
        try:
            assets[key] = asset(*future.result())
        except Exception:
            pass  # an undecodable image is skipped rather than failing the whole book

    with ThreadPoolExecutor(WORKERS) as pool:
        for picture in pictures:
            key = picture.key
            if key in assets or key in pending:
                continue
            try:
                ready, image = source(doc, picture)
            except Exception:
                continue
            if ready:
                assets[key] = asset(*ready)
            elif image is not None:
                pending[key] = pool.submit(encode, image)
            if len(pending) >= IN_FLIGHT:
                done, _ = wait(pending.values(), return_when=FIRST_COMPLETED)
                for finished in [k for k, f in pending.items() if f in done]:
                    finish(finished, pending.pop(finished))
        for key, future in pending.items():
            finish(key, future)
    return assets
