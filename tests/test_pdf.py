import io
import random
import re
import tempfile
import unittest
from xml.etree import ElementTree as ET
import zipfile

import pymupdf
from PIL import Image

from kindle_drop.app import App, epub_metadata
from kindle_drop.pdf import build_pdf
from kindle_drop.pdf.text import Vocabulary, ligature_map, replacements, should_dehyphenate

LEFT, WIDTH, BODY, LEADING = 72, 400, 11, 14
PROSE = ('Software design is a continuous process that spans the entire lifecycle of a system. '
         'Good designers keep the interfaces simple and hide the complexity inside deep modules, '
         'which separates the concerns of callers from the details of the implementation. ')


class BookWriter:
    """Typesets text the way a print book does: justified width, indents, running heads."""

    def __init__(self):
        self.doc = pymupdf.open()
        self.page, self.y, self.hyphenated = None, 0, []
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=540, height=720)
        number = self.doc.page_count
        self.page.insert_text((LEFT, 40), 'A Sample Book', fontname='tiro', fontsize=9)
        self.page.insert_text((265, 700), str(number), fontname='tiro', fontsize=9)
        self.y = 90

    def line(self, x, text, font='tiro', size=BODY, leading=LEADING):
        if self.y > 640:
            self.new_page()
        self.page.insert_text((x, self.y), text, fontname=font, fontsize=size)
        self.y += leading

    def fits(self, text, x):
        return pymupdf.get_text_length(text, fontname='tiro', fontsize=BODY) <= WIDTH - (x - LEFT)

    def paragraph(self, text, indent=18, hyphenate=False):
        """Greedy line breaking; with hyphenate, long overflowing words are split after 4 letters."""
        line, x = '', LEFT + indent
        for word in text.split():
            candidate = f'{line} {word}'.strip()
            if self.fits(candidate, x):
                line = candidate
                continue
            if hyphenate and len(word) >= 8 and word.isalpha() and self.fits(f'{line} {word[:4]}-', x):
                self.line(x, f'{line} {word[:4]}-')
                self.hyphenated.append(word)
                line, x = word[4:], LEFT
                continue
            self.line(x, line)
            line, x = word, LEFT
        self.line(x, line)

    def bytes(self):
        return self.doc.tobytes()


def sample_book():
    book = BookWriter()
    book.line(LEFT, 'Chapter 1 Deep Modules', font='hebo', size=18, leading=30)
    book.paragraph(PROSE * 2)
    for _ in range(4):
        book.paragraph(PROSE * 2)
    book.paragraph('This paragraph checks hyphenation. ' + PROSE * 3, hyphenate=True)
    book.line(LEFT + 18, 'int open(const char* path, int flags);', font='cour', size=9, leading=11)
    book.line(LEFT + 18 + pymupdf.get_text_length('    ', fontname='cour', fontsize=9),
              'return fd;', font='cour', size=9, leading=18)
    for item in ('How are files represented on disk?', 'How are directories stored?'):
        book.page.draw_rect(pymupdf.Rect(LEFT + 8, book.y - 6, LEFT + 12, book.y - 2), color=None, fill=(0, 0, 0))
        book.line(LEFT + 18, item)
    book.paragraph('The final paragraph ends the chapter.')
    return book


def sample_pdf():
    return sample_book().bytes()


def html_of(data):
    return build_pdf(data).html


def paragraphs(html):
    return [re.sub(r'<[^>]+>', '', p) for p in re.findall(r'<p>(.*?)</p>', html)]


class TextRepairTests(unittest.TestCase):
    def test_ligature_glyph_is_repaired_per_word(self):
        lines = ['we define errors', 'a different layer', 'De˵ne errors', 'Di˵erent Layer']
        vocabulary = Vocabulary(lines)
        mapping = ligature_map(lines, vocabulary)
        fixed = []
        for line in lines[2:]:
            chosen = replacements(line, mapping, vocabulary)
            fixed.append(''.join(chosen.get(i, c) for i, c in enumerate(line)))
        self.assertEqual(fixed, ['Define errors', 'Different Layer'])

    def test_dehyphenation_uses_document_vocabulary(self):
        vocabulary = Vocabulary(['this separates concerns', 'a pass-through method'])
        self.assertTrue(should_dehyphenate('it sepa-', 'rates them', vocabulary))
        self.assertFalse(should_dehyphenate('the pass-', 'through method', vocabulary))
        self.assertFalse(should_dehyphenate('ends with a dash -', 'Next', vocabulary))


class PdfReflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = sample_book()
        cls.html = html_of(cls.book.bytes())

    def test_lines_and_page_breaks_reflow_into_paragraphs(self):
        prose = [p for p in paragraphs(self.html) if 'Software design' in p]
        self.assertEqual(len(prose), 6)  # one per typeset paragraph, including the one crossing pages
        for paragraph in prose:
            self.assertGreater(len(paragraph), 300)
            self.assertTrue(paragraph.rstrip().endswith('implementation.'), paragraph[-60:])

    def test_running_heads_page_numbers_and_hyphens_are_removed(self):
        self.assertNotIn('A Sample Book', self.html)
        self.assertNotRegex(self.html, r'<p>\d+</p>')
        self.assertTrue(self.book.hyphenated)
        hyphenated = paragraphs(self.html)[5]
        for word in self.book.hyphenated:
            self.assertIn(word, hyphenated)
            self.assertNotIn(word[:4] + '-', hyphenated)

    def test_structure_headings_code_and_lists(self):
        self.assertIn('<h1 class="chapter" id="h0">Chapter 1 Deep Modules</h1>', self.html)
        self.assertIn('<pre><code>int open(const char* path, int flags);\n    return fd;</code></pre>', self.html)
        self.assertIn('<ul>\n<li>How are files represented on disk?</li>\n<li>How are directories stored?</li>\n</ul>',
                      self.html)

    def test_scanned_pdf_is_rejected(self):
        doc = pymupdf.open()
        doc.new_page().draw_rect(pymupdf.Rect(10, 10, 200, 200), fill=(0.5, 0.5, 0.5))
        with self.assertRaisesRegex(ValueError, 'no text layer'):
            html_of(doc.tobytes())


def picture_pdf():
    """One text page with a large photo-like image and a small flat diagram."""
    random.seed(7)
    photo = Image.new('RGB', (3000, 2000))
    photo.putdata([(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(3000 * 2000)])
    diagram = Image.new('RGB', (600, 400), 'white')
    diagram.paste((20, 60, 200), (100, 100, 500, 300))
    book = BookWriter()
    book.paragraph(PROSE * 3)
    for image, rect in ((photo, (72, 300, 472, 560)), (diagram, (72, 580, 372, 680))):
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        book.page.insert_image(pymupdf.Rect(*rect), stream=buffer.getvalue())
    return book.bytes()


class EpubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = build_pdf(picture_pdf(), 'Pictures')
        cls.epub = cls.book.epub('urn:test')

    def test_epub_container_is_valid_and_deterministic(self):
        self.assertEqual(self.book.epub('urn:test'), self.epub)
        with zipfile.ZipFile(io.BytesIO(self.epub)) as z:
            names = z.namelist()
            self.assertEqual(names[0], 'mimetype')
            self.assertEqual(z.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
            for name in names:
                if name.endswith(('.xhtml', '.opf', '.ncx', '.xml')):
                    ET.fromstring(z.read(name))  # well-formed XML, as EPUB readers require
            nav = z.read('OEBPS/nav.xhtml').decode()
        self.assertIn('epub:type="toc"', nav)
        self.assertEqual(epub_metadata(self.epub), ('Pictures', 'Unknown author'))

    def test_images_are_sized_and_encoded_for_e_readers(self):
        kinds = {}
        for name, data in self.book.assets.items():
            with Image.open(io.BytesIO(data)) as image:
                self.assertLessEqual(image.width, 1264)
                kinds[image.format] = image.size
        self.assertEqual(set(kinds), {'JPEG', 'PNG'})  # photo → JPEG, flat diagram → palette PNG
        self.assertEqual(kinds['JPEG'][0], 1264)
        self.assertLess(len(self.epub), 2_000_000)


class PdfAppTests(unittest.TestCase):
    def test_pdf_becomes_queued_epub_once(self):
        with tempfile.TemporaryDirectory() as data:
            app = App(data)
            pdf = sample_pdf()
            book = app.add_file(pdf, 'Sample book')
            self.assertEqual(app.add_file(pdf, 'Sample book')['id'], book['id'])
            epub = (app.data / 'books' / book['filename']).read_bytes()
            self.assertEqual(epub_metadata(epub)[0], 'Sample book')
            with zipfile.ZipFile(io.BytesIO(epub)) as z:
                text = ''.join(z.read(n).decode() for n in z.namelist() if n.endswith(('.xhtml', '.html')))
            self.assertIn('Chapter 1 Deep Modules', text)
            self.assertIn('which separates the concerns', text)
