"""Reflow text-layer PDFs into clean HTML for EPUB conversion, without OCR or ML.

Pipeline: extract positioned spans → repair glyphs with the book's own vocabulary →
rebuild paragraphs, lists, code and asides from geometry → render semantic HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading

from .extract import open_pdf, read_document, render_cover
from .layout import reflow
from .epub import epub_bytes
from .render import chapters, document
from .text import Vocabulary, guess_language, ligature_map, replacements

MIN_TEXT = 200  # characters; less than this means a scanned PDF
MUPDF = threading.Lock()  # PyMuPDF must not be used from several threads at once


@dataclass
class PdfBook:
    chapters: list
    title: str
    author: str
    language: str
    assets: dict[str, bytes]  # images/... referenced by the chapters
    cover: bytes | None  # JPEG
    warnings: list[str] = field(default_factory=list)

    @property
    def images(self):
        return len(self.assets)

    @property
    def html(self):
        """The whole book as one HTML page, for review and tests (images are not inlined)."""
        return document(self.chapters, self.title, self.language)

    def epub(self, identifier):
        return epub_bytes(self.chapters, self.title, self.author, self.language, identifier, self.assets, self.cover)


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


def build_pdf(data, name=None):
    """Reflow PDF bytes into chapters and e-reader sized images, all in memory.

    name is the file stem, used as title when the PDF metadata has none.
    """
    with MUPDF, open_pdf(data) as doc:
        cover, metadata = render_cover(doc), doc.metadata or {}
        pages, assets = read_document(doc)
    if sum(len(line.text.strip()) for page in pages for line in page.lines) < MIN_TEXT:
        raise ValueError('This PDF has no text layer (it is probably scanned). OCR is not supported.')
    if sum(len(line.text.strip()) for line in pages[0].lines) < 40:
        pages = pages[1:]  # the cover page itself; it is already the EPUB cover
    kept = {picture.key for page in pages for picture in page.pictures}
    assets = {key: value for key, value in assets.items() if key in kept}
    vocabulary = repair_glyphs(pages)
    blocks = reflow(pages)
    first_heading = next((' '.join(l.text.split()) for b in blocks if b.kind == 'heading' for l in b.lines[:1]), '')
    file_title = ' '.join((name or '').replace('_', ' ').split())
    title = (metadata.get('title') or '').strip() or file_title or first_heading or 'PDF document'
    author = (metadata.get('author') or '').strip() or 'Unknown author'
    sections = chapters(blocks, vocabulary, lambda picture: assets.get(picture.key, (None,))[0])
    return PdfBook(sections, title, author, guess_language(vocabulary), dict(assets.values()), cover)
