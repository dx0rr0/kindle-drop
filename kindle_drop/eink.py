"""Size and encode images for e-ink readers; shared by PDFs and web articles.

Kindle screens show 16 gray levels, so color only costs bytes and transfer time. Images
are downscaled to an e-reader screen (1264 px wide covers 300 ppi Kindles); photographs and
screenshots become JPEG, flat line art becomes a lossless gray PNG.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image

MAX_SIZE = (1264, 1680)
JPEG_QUALITY = 80
FLAT_LEVELS = 64  # at most this many gray levels: line art, kept lossless as PNG


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


def prepare(image):
    """A gray copy that fits the screen."""
    image.draft('L', MAX_SIZE)  # JPEG: decode directly to gray at a reduced scale
    image = gray(image)
    image.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    return image


def png(image):
    buffer = BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def save(image):
    """Return (bytes, extension) for a prepared image."""
    if image.getcolors(FLAT_LEVELS) is not None:  # diagram or icon: lossless and crisp
        return png(image), 'png'
    buffer = BytesIO()
    image.save(buffer, 'JPEG', quality=JPEG_QUALITY)
    return buffer.getvalue(), 'jpg'


def encode(image):
    """Return (bytes, extension) for a decoded image."""
    return save(prepare(image))
