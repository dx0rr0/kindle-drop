"""Verify semantic formatting and offline images in the final converted EPUB."""
from io import BytesIO
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from xml.etree import ElementTree as ET
import zipfile

from PIL import Image
from lxml import html
from kindle_drop.app import App
from kindle_drop.articles import build_article, content_tree, download_image, image_source, normalize_image


def image_bytes():
    output = BytesIO()
    Image.new('RGB', (800, 400), 'white').save(output, format='PNG')
    return output.getvalue()


def photo_bytes(size=(1600, 900), format='JPEG'):
    """Noise has thousands of gray levels, like a photograph."""
    output = BytesIO()
    Image.effect_noise(size, 64).convert('RGB').save(output, format=format)
    return output.getvalue()


ARTICLE = '''<html lang="en"><head><title>A readable article</title></head><body>
<nav>Website navigation that must disappear</nav><article><header><h1>A readable article</h1></header>
<p>Introductory article text with enough meaningful prose to identify this as a complete article.
We preserve its structure and make it comfortable to read on an e-ink device away from the computer.</p>
<h2 id="details"><button>Copy link to heading</button>Details</h2>
<p>Keep <strong>bold emphasis</strong>, <em>italic emphasis</em> and <code>inlineCode()</code>.</p>
<ul><li>First item</li><li>Second item</li></ul><blockquote>A quoted paragraph.</blockquote>
<pre><code>const first = 1;
const second = 2;</code></pre>
<figure><img alt="A useful diagram" src="/large.png" srcset="/diagram.png 1x, /large.png 2x">
<img alt="Dark variant" src="/dark.png" class="[.light-theme_&amp;]:hidden!">
<img alt="Mobile variant" src="/mobile.png" class="hidden responsive:block">
<figcaption>The original caption.</figcaption></figure>
<p><img alt="Lazy image" data-src="/lazy.png"></p>
<table><tr><th>Feature</th><th>Result</th></tr><tr><td>Images</td><td>Offline</td></tr></table>
<p><a href="/reference">Reference</a> <a href="javascript:bad()">Unsafe link</a></p>
<script>bad()</script><iframe src="http://127.0.0.1"></iframe>
</article><footer>Unrelated footer</footer></body></html>'''


class ArticleTests(unittest.TestCase):
    def test_final_epub_preserves_structure_and_embeds_local_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = App(Path(temporary) / 'state')
            fetched = []
            def fetch(url, **kwargs):
                fetched.append(url)
                return image_bytes(), 'image/png', url
            with patch('kindle_drop.app.fetch_public', fetch):
                book = app.article('https://example.org/article', ARTICLE.encode())
            self.assertCountEqual(fetched, ['https://example.org/diagram.png', 'https://example.org/lazy.png'])
            self.assertFalse(book['warnings'])
            with zipfile.ZipFile(app.data / 'books' / book['filename']) as archive:
                files = set(archive.namelist())
                pages = [(name, ET.fromstring(archive.read(name))) for name in files if name.endswith(('.html', '.xhtml'))]
                text = '\n'.join(''.join(tree.itertext()) for _, tree in pages)
                self.assertIn('Details', text)
                self.assertIn('The original caption.', text)
                self.assertIn('const first = 1;\nconst second = 2;', text)
                self.assertNotIn('Copy link to heading', text)
                self.assertNotIn('Website navigation', text)
                self.assertNotIn('Unrelated footer', text)
                for tag in ('h2', 'ul', 'li', 'blockquote', 'pre', 'code', 'table'):
                    self.assertTrue(any(tree.findall('.//{*}' + tag) for _, tree in pages), tag)
                images = [(name, image) for name, tree in pages for image in tree.findall('.//{*}img')]
                self.assertGreaterEqual(len(images), 2)
                for name, image in images:
                    src = image.get('src')
                    self.assertNotIn('://', src)
                    # Check each image's relative reference against actual ZIP contents.
                    normalized = str(PurePosixPath(name).parent / unquote(src))
                    parts = []
                    for part in normalized.split('/'):
                        if part == '..':
                            parts.pop()
                        elif part != '.':
                            parts.append(part)
                    self.assertIn('/'.join(parts), files)
                styles = '\n'.join(archive.read(n).decode() for n in files if n.endswith('.css'))
                self.assertIn('pre-wrap', styles)
                self.assertIn('line-height', styles)

    def test_failed_image_keeps_link_and_reports_warning(self):
        def fail(*args, **kwargs):
            raise ValueError('Private IP rejected')
        article = build_article(ARTICLE.encode(), 'https://example.org/article', fail)
        tree = html.fromstring(article.section.html)
        self.assertEqual(article.images, 0)
        self.assertTrue(article.warnings)
        self.assertFalse(tree.findall('.//img'))
        self.assertIn('[Image: A useful diagram]', tree.text_content())
        self.assertTrue(tree.xpath('//a[@href="https://example.org/diagram.png"]'))
        self.assertTrue(article.cover.startswith(b'\xff\xd8'))  # a text-only cover still names the article

    def test_active_content_and_external_style_are_removed(self):
        tree, _ = content_tree(ARTICLE.replace('<p>Introductory', '<p style="background:url(http://127.0.0.1)" onclick="bad()">Introductory').encode(), 'https://example.org/article')
        self.assertFalse(tree.xpath('//script | //iframe | //button | //@style | //@onclick'))
        self.assertFalse(tree.xpath('//a[starts-with(@href,"javascript:")]'))

    def test_image_fetch_is_bounded_and_uses_public_fetcher(self):
        seen = []
        def fake(url, **kwargs):
            seen.append((url, kwargs))
            return image_bytes(), 'image/png', url
        original, (normalized, extension, size) = download_image('https://example.org/image.png', fake)
        self.assertEqual(seen[0][1], {'max_bytes': 8 * 1024 * 1024, 'timeout': 12})
        self.assertEqual(original, len(image_bytes()))
        self.assertTrue(normalized.startswith(b'\x89PNG'))
        self.assertEqual((extension, size), ('png', (800, 400)))
        with self.assertRaises(ValueError):
            download_image('data:image/svg+xml;base64,PHN2Zz4=', fake)


class TitleTests(unittest.TestCase):
    def test_site_suffix_and_permalink_marks_are_dropped(self):
        from kindle_drop.articles import clean_title
        self.assertEqual(clean_title('E Ink - Wikipedia', 'en.wikipedia.org'), 'E Ink')
        self.assertEqual(clean_title('Frontier Pre-training | Microsoft AI', 'microsoft.ai'), 'Frontier Pre-training')
        self.assertEqual(clean_title('The calibre Content server¶', 'manual.calibre-ebook.com'), 'The calibre Content server')
        self.assertEqual(clean_title('Pricing | Acme Blog', 'acme.dev', 'Acme Blog'), 'Pricing')
        # Unrelated suffixes and short words are part of the title.
        self.assertEqual(clean_title('Rust vs Go - A Comparison', 'example.org'), 'Rust vs Go - A Comparison')
        self.assertEqual(clean_title('Ideas | AI', 'aiweekly.com'), 'Ideas | AI')

    def test_hyphens_are_normalized_like_calibre(self):
        from kindle_drop.articles import section
        node = html.fromstring('<div><p>top' + chr(0x2011) + '1 grass' + chr(0xad) + 'hopper</p></div>')
        self.assertEqual(section(node, node, 'Title').html, '<div><p>top-1 grasshopper</p></div>')


class ArticleImageTests(unittest.TestCase):
    def test_srcset_urls_may_contain_commas(self):
        # Substack's CDN puts resize options, separated by commas, inside each URL.
        cdn = 'https://substackcdn.com/image/fetch/w_{0},c_limit,f_webp,q_auto:good/https%3A%2F%2Fexample.org%2Fa.png'
        node = html.fromstring(f'<img src="/fallback.png" srcset="{cdn.format(424)} 424w, {cdn.format(848)} 848w, {cdn.format(1456)} 1456w, {cdn.format(2400)} 2400w">')
        self.assertEqual(image_source(node, 'https://example.org/p'), cdn.format(1456))
        plain = html.fromstring('<img srcset="/a.png 1x,/b.png 2x">')
        self.assertEqual(image_source(plain, 'https://example.org/p'), 'https://example.org/a.png')

    def test_images_are_gray_screen_sized_and_compact(self):
        data, extension, size = normalize_image(photo_bytes((2400, 1350)))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual((image.format, image.mode, image.size), ('JPEG', 'L', (1264, 711)))
        self.assertEqual((extension, size), ('jpg', (1264, 711)))
        # A lossless source keeps PNG when that is not much larger (crisp screenshot text).
        flat = BytesIO()
        Image.new('RGB', (900, 300), 'white').save(flat, 'PNG')
        self.assertEqual(normalize_image(flat.getvalue())[1], 'png')

    def test_first_suitable_image_becomes_the_cover(self):
        page = ARTICLE.replace('<p>Introductory', '<p><img src="/icon.png" alt="Tiny icon">Introductory')
        def fetch(url, **kwargs):
            if url.endswith('icon.png'):
                output = BytesIO()
                Image.new('RGB', (40, 40), 'black').save(output, 'PNG')
                return output.getvalue(), 'image/png', url
            return photo_bytes(), 'image/jpeg', url
        article = build_article(page.encode(), 'https://example.org/article', fetch)
        with Image.open(BytesIO(article.cover)) as cover:
            self.assertEqual((cover.format, cover.mode, cover.size), ('JPEG', 'L', (1072, 1448)))
            top = cover.crop((0, 0, 1072, 400))
            self.assertGreater(len(top.getcolors(256) or range(256)), 64)  # the photo, not the tiny icon
        with zipfile.ZipFile(BytesIO(article.epub())) as archive:
            opf = archive.read('OEBPS/content.opf').decode()
            self.assertIn('properties="cover-image"', opf)
            self.assertNotIn('cover.xhtml', opf)  # reading starts at the article, not a cover page
            nav = archive.read('OEBPS/nav.xhtml').decode()
            self.assertIn('A readable article', nav)
            self.assertIn('Details', nav)

    def test_without_article_images_the_preview_card_is_the_cover(self):
        page = ARTICLE.replace('<head>', '<head><meta property="og:image" content="/card.jpg">')
        page = page.replace('<figure>', '<!--').replace('</figure>', '-->').replace('<p><img alt="Lazy image" data-src="/lazy.png"></p>', '')
        fetched = []
        def fetch(url, **kwargs):
            fetched.append(url)
            return photo_bytes((1200, 630)), 'image/jpeg', url
        article = build_article(page.encode(), 'https://example.org/article', fetch)
        self.assertEqual(fetched, ['https://example.org/card.jpg'])
        self.assertEqual(article.images, 0)  # used for the cover only, not inserted in the text
        self.assertIsNotNone(article.cover)
