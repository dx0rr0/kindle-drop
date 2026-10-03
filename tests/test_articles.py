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
from kindle_drop.articles import build_article, content_tree, download_image


def image_bytes():
    output = BytesIO()
    Image.new('RGB', (800, 400), 'white').save(output, format='PNG')
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
        with tempfile.TemporaryDirectory() as temporary:
            def fail(*args, **kwargs):
                raise ValueError('Private IP rejected')
            text, _, _, count, warnings = build_article(ARTICLE.encode(), 'https://example.org/article', temporary, fail)
            tree = html.fromstring(text)
            self.assertEqual(count, 0)
            self.assertTrue(warnings)
            self.assertFalse(tree.findall('.//img'))
            self.assertIn('[Image: A useful diagram]', tree.text_content())
            self.assertTrue(tree.xpath('//a[@href="https://example.org/diagram.png"]'))

    def test_active_content_and_external_style_are_removed(self):
        tree, _ = content_tree(ARTICLE.replace('<p>Introductory', '<p style="background:url(http://127.0.0.1)" onclick="bad()">Introductory').encode(), 'https://example.org/article')
        self.assertFalse(tree.xpath('//script | //iframe | //button | //@style | //@onclick'))
        self.assertFalse(tree.xpath('//a[starts-with(@href,"javascript:")]'))

    def test_image_fetch_is_bounded_and_uses_public_fetcher(self):
        seen = []
        def fake(url, **kwargs):
            seen.append((url, kwargs))
            return image_bytes(), 'image/png', url
        original, normalized = download_image('https://example.org/image.png', fake)
        self.assertEqual(seen[0][1], {'max_bytes': 8 * 1024 * 1024, 'timeout': 12})
        self.assertTrue(normalized.startswith(b'\x89PNG'))
        with self.assertRaises(ValueError):
            download_image('data:image/svg+xml;base64,PHN2Zz4=', fake)
