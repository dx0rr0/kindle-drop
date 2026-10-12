"""Reflow text-layer PDFs into clean HTML for EPUB conversion, without OCR or ML.

Pipeline: extract positioned spans → repair glyphs with the book's own vocabulary →
rebuild paragraphs, lists, code and asides from geometry → render semantic HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import threading

from .extract import open_pdf, read_document, render_cover
from .layout import reflow
from .render import document
from .text import Vocabulary, ligature_map, replacements

MIN_TEXT = 200  # characters; less than this means a scanned PDF
MUPDF = threading.Lock()  # PyMuPDF must not be used from several threads at once


@dataclass
class PdfBook:
    html: str
    title: str
    author: str
    cover: Path | None
    images: int
    warnings: list[str] = field(default_factory=list)


def repair_glyphs(pages):
    lines = [line.text for page in pages for line in page.lines]
    vocabulary = Vocabulary(lines)
    mapping = ligature_map(lines, vocabulary)
    if mapping:
        for page in pages:
            for line in page.lines:
                # Decide on whole words: a broken glyph often sits at a span boundary.
                chosen = replacements(line.text, mapping, vocabulary)
                position = 0
                for span in line.spans:
                    span.text, position = ''.join(chosen.get(position + i, c) for i, c in enumerate(span.text)), position + len(span.text)
    return Vocabulary([line.text for page in pages for line in page.lines])


def write_pictures(blocks, folder):
    names = {}
    for block in blocks:
        if block.picture is not None:
            name = 'images/' + hashlib.sha256(block.picture.png).hexdigest()[:20] + '.png'
            target = Path(folder) / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(block.picture.png)
            names[id(block.picture)] = name
    return names


def build_pdf(data, folder, name=None):
    """Convert PDF bytes to HTML in folder (with images/ and cover.jpg); name is the file stem."""
    with MUPDF, open_pdf(data) as doc:
        pages = read_document(doc)
        if sum(len(line.text.strip()) for page in pages for line in page.lines) < MIN_TEXT:
            raise ValueError('This PDF has no text layer (it is probably scanned). OCR is not supported.')
        cover = Path(folder) / 'cover.jpg'
        render_cover(doc, cover)
        if sum(len(line.text.strip()) for line in pages[0].lines) < 40:
            pages = pages[1:]  # the cover page itself; it is already the EPUB cover
        metadata = doc.metadata or {}
    vocabulary = repair_glyphs(pages)
    blocks = reflow(pages)
    names = write_pictures(blocks, folder)
    first_heading = next((' '.join(l.text.split()) for b in blocks if b.kind == 'heading' for l in b.lines[:1]), '')
    file_title = ' '.join((name or '').replace('_', ' ').split())
    title = (metadata.get('title') or '').strip() or file_title or first_heading or 'PDF document'
    author = (metadata.get('author') or '').strip() or 'Unknown author'
    html = document(blocks, title, vocabulary, lambda picture: names[id(picture)])
    return PdfBook(html, title, author, cover, len(set(names.values())))
