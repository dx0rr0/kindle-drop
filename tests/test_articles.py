"""Verify semantic formatting and offline images in the final converted EPUB."""
import base64
from html import escape as html_escape
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
from kindle_drop.articles import (build_article, content_tree, download_image, find_graphics, graphic_parts,
                                  image_source, normalize_image)
from kindle_drop.browser import find_browser
from kindle_drop.formulas import MATHML


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
        tree, _, _ = content_tree(ARTICLE.replace('<p>Introductory', '<p style="background:url(http://127.0.0.1)" onclick="bad()">Introductory').encode(), 'https://example.org/article')
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

    def test_figures_survive_pages_without_an_article_element(self):
        # Text extraction drops <figure> outside <article>, with its image and caption.
        prose = ''.join(f'<p>Paragraph {n} carries enough article prose about review layers, checks and '
                        'approvals to be read as the main text of this page by the extractor.</p>' for n in range(5))
        page = (f'<html lang="en"><head><title>Review layers</title></head><body><div class="prose"><h1>Review layers</h1>'
                f'<section>{prose}<figure class="art-fig"><img class="fg" src="/media/diagram.png" alt="A diagram">'
                f'<figcaption>FIG A The path of a change</figcaption></figure>{prose}</section></div></body></html>')
        article = build_article(page.encode(), 'https://example.org/post', lambda url, **kw: (photo_bytes(), 'image/jpeg', url))
        self.assertEqual(article.images, 1)
        chapter = article.section.html
        self.assertIn('FIG A The path of a change', chapter)
        self.assertNotIn('<p><figure', chapter)
        self.assertLess(chapter.index('Paragraph 4'), chapter.index('<figure'))

    def test_pages_without_an_article_element_keep_headings_lists_and_code(self):
        # Outside <article>, text extraction keeps loose paragraphs only; the element holding them is kept whole.
        prose = ''.join(f'<p>Paragraph {n} carries enough article prose about evals, graders and '
                        'hillclimbing to be read as the main text of this page by the extractor.</p>' for n in range(4))
        page = ('<html lang="en"><head><title>Evals</title></head><body><nav><a href="/">Home</a></nav>'
                '<header><h1>Evals</h1><p class="standfirst">Principles for designing evals and hillclimbing against them.</p></header>'
                '<aside><a href="#steps">Steps</a></aside><div class="prose"><section>'
                f'{prose}<h2 id="steps">/CLAUDE-API BUILD-EVAL</h2><p>The <code>build-eval</code> command turns these principles into a workflow.</p>'
                '<ul><li>Production transcripts, after asking about retention.</li><li>Bug reports and support tickets.</li></ul>'
                '<div class="art-code"><div class="ch"><span>CODE<span>Shell</span></span><button>Copy</button></div><pre>claude update</pre></div>'
                f'<figure><img src="/fig.png" alt="Curves"><figcaption><b>FIG 2</b><span>Adversarial sampling</span></figcaption></figure>'
                f'<blockquote><span>@<!-- -->ClaudeDevs</span> Long answers now stream smoother.</blockquote>'
                f'{prose}</section></div><footer><p>Copyright notice for every page on this site, which is not the article.</p></footer></body></html>')
        article = build_article(page.encode(), 'https://example.org/post', lambda url, **kw: (photo_bytes(), 'image/jpeg', url))
        chapter = article.section.html
        self.assertIn('<h2 id="steps">/CLAUDE-API BUILD-EVAL</h2>', chapter)
        self.assertIn('<p>The <code>build-eval</code> command', chapter)
        self.assertIn('<li>Bug reports and support tickets.</li>', chapter)
        self.assertIn('<pre>claude update</pre>', chapter)
        self.assertNotIn('Copy', chapter)
        self.assertNotIn('Shell', chapter)
        self.assertIn('FIG 2 Adversarial sampling', chapter.replace('</b>', '').replace('<b>', ''))
        self.assertLess(chapter.index('Principles for designing evals'), chapter.index('Paragraph 0'))
        self.assertIn('@ClaudeDevs Long answers', chapter)  # text after a React <!-- --> comment
        self.assertNotIn('Copyright notice', chapter)
        self.assertNotIn('Home', chapter)
        self.assertEqual(article.images, 1)

    def test_image_addresses_with_spaces_and_non_ascii_are_encoded(self):
        node = html.fromstring('<img src="/uploads/Screenshot at 5.04.51 PM.png">')
        self.assertEqual(image_source(node, 'https://example.org/post'),
                         'https://example.org/uploads/Screenshot%20at%205.04.51%E2%80%AFPM.png')
        node = html.fromstring('<img src="/i.png?w=600&amp;q=80%2C">')
        self.assertEqual(image_source(node, 'https://example.org/post'), 'https://example.org/i.png?w=600&q=80%2C')

    def test_article_covers_carry_a_tab_and_books_do_not(self):
        fetch = lambda url, **kwargs: (photo_bytes(), 'image/jpeg', url)
        covers = [Image.open(BytesIO(build_article(ARTICLE.encode(), 'https://example.org/article', fetch, article=kind).cover))
                  for kind in (True, False)]
        tab = (40, 44, 400, 148)
        labeled, plain = (cover.crop(tab) for cover in covers)
        self.assertLess(sum(v < 40 for v in labeled.getdata()), labeled.width * labeled.height)
        self.assertGreater(sum(v < 40 for v in labeled.getdata()), labeled.width * labeled.height // 3)  # solid ink
        self.assertNotEqual(labeled.tobytes(), plain.tobytes())
        self.assertEqual(covers[0].crop((0, 400, 1072, 1448)).tobytes(), covers[1].crop((0, 400, 1072, 1448)).tobytes())


PROSE = ''.join(f'<p>Paragraph {n} explains the training run in enough detail to read as article prose, '
                'with several sentences about models, data and the evaluation results that follow.</p>' for n in range(6))
# A scripted widget: its chart exists only once its script runs in a browser.
WIDGET = ('<p>Figure 1: Throughput by model.</p><div id="chart"></div><script>'
          'document.getElementById("chart").innerHTML = "<div style=\'height:300px;background:#000\'></div>";</script>')
BARS = ('<section aria-label="Latency by version"><h3>Cold start</h3>'
        + ''.join(f'<div><span>Version {n}: {w} ms</span><div style="background:#555;height:20px;width:{w}%"></div></div>'
                  for n, w in ((1, 90), (2, 40), (3, 10))) + '</section>')
SVG = '<svg viewBox="0 0 300 150"><rect width="300" height="150" fill="#ccc"/><text x="10" y="20">Loss</text><text x="10" y="140">Step</text></svg>'


def widget(markup, kind='template'):
    if kind == 'srcdoc':
        return f'<iframe srcdoc="{html_escape(markup)}"></iframe>'
    encoded = base64.b64encode(markup.encode()).decode()
    # As on microsoft.ai: "widget" containers read as boilerplate to text extraction.
    return f'<section class="block-widget-section"><block-widget class="widget"><template data-widget-html>{encoded}</template></block-widget></section>'


def chart_page(*blocks):
    return ('<html lang="en"><head><title>Training report</title></head><body><main><h1>Training report</h1>'
            + PROSE[:len(PROSE) // 2] + ''.join(blocks) + PROSE[len(PROSE) // 2:] + '</main></body></html>').encode()


class GraphicTests(unittest.TestCase):
    def test_charts_are_found_and_survive_extraction(self):
        page = chart_page(widget(WIDGET), widget('<p>Figure 2: Loss.</p><canvas></canvas><script>1</script>', 'srcdoc'),
                          BARS, SVG, widget('<table><tr><th>Layers</th><td>78</td></tr></table>'))
        tree = html.fromstring(page)
        graphics = find_graphics(tree)
        self.assertEqual([(g.markup is None, g.alt) for g in graphics],
                         [(False, 'Figure 1: Throughput by model.'), (False, 'Figure 2: Loss.'), (True, 'Chart'),
                          (True, 'Latency by version')])
        body, _, _ = content_tree(page, 'https://example.org/report')
        self.assertEqual([i.get('src') for i in body.iter('img')], [f'kd-graphic:{n}.png' for n in (0, 1, 3, 2)])
        self.assertIn('Layers', ' '.join(t.text_content() for t in body.iter('table')))  # a table widget is text

    def test_without_a_browser_charts_keep_their_captions(self):
        with patch('kindle_drop.browser.find_browsers', return_value=[]):
            article = build_article(chart_page(widget(WIDGET)), 'https://example.org/report', lambda *a, **k: 1 / 0)
        tree = html.fromstring(article.section.html)
        self.assertEqual(article.images, 0)
        self.assertIn('[Chart: Figure 1: Throughput by model.]', tree.text_content())
        self.assertTrue(tree.xpath('//figcaption/a[@href="https://example.org/report"]'))
        self.assertIn('Install Microsoft Edge or Google Chrome', ' '.join(article.warnings))

    def render_report(self, fetch):
        """What the browser returned for each chart, for a failure message (CI has no screen)."""
        import traceback
        from kindle_drop import articles, browser
        page = html.fromstring(chart_page(widget(WIDGET), BARS))
        graphics = find_graphics(page)
        documents = [(g.markup, [None]) for g in graphics if g.markup is not None]
        documents.append((articles.page_document(page, 'https://example.org/report', fetch),
                          [f'[data-kd-graphic="{g.number}"]' for g in graphics if g.markup is None]))
        try:
            report = []
            for found in browser.render(documents):
                for shot in found:
                    if not shot:
                        report.append(repr(shot))
                        continue
                    try:
                        report.append(f'{len(shot[0])} bytes, {len(graphic_parts(shot[0]))} pieces')
                    except Exception as error:
                        report.append(f'{len(shot[0])} bytes, parts failed: {error!r}')
            return f'{find_browser()}: {report}'
        except Exception:
            return f'{find_browser()}: {traceback.format_exc()}'

    @unittest.skipUnless(find_browser(), 'no Chromium-based browser is installed')
    def test_charts_render_offline_as_gray_images(self):
        fetched = []
        def fetch(url, **kwargs):
            fetched.append(url)
            raise ValueError('offline')
        article = build_article(chart_page(widget(WIDGET), BARS), 'https://example.org/report', fetch)
        if article.warnings:
            self.fail(f'{article.warnings}: {self.render_report(fetch)}')
        self.assertEqual(fetched, [])  # no stylesheets to inline, and rendering itself never fetches
        tree = html.fromstring(article.section.html)
        figures = tree.findall('.//figure')
        self.assertEqual(len(figures), 2)
        self.assertEqual(figures[0].findtext('figcaption'), 'Figure 1: Throughput by model.')
        self.assertEqual(article.images, 2)
        for data in article.assets.values():
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.mode, 'L')
                self.assertLess(image.getextrema()[0], 40)  # the drawn bar or box is there
                self.assertEqual(image.getpixel((0, 0)), 255)

    def test_tall_charts_are_cut_at_blank_rows_onto_white_paper(self):
        chart = Image.new('RGB', (1000, 4000), '#fdf8ec')  # cream page background
        for top in range(100, 3500, 600):
            chart.paste((40, 40, 40), (100, top, 900, top + 450))
        output = BytesIO()
        chart.save(output, 'PNG')
        pieces = graphic_parts(output.getvalue())
        self.assertGreaterEqual(len(pieces), 3)
        for data, _, (width, height) in pieces:
            self.assertLessEqual(height, round(width * 1.3 * 1.25))
            with Image.open(BytesIO(data)) as piece:
                self.assertEqual(piece.getpixel((0, 0)), 255)  # cream became white
                # Cuts fall between blocks: no piece starts or ends inside one.
                self.assertEqual(piece.getpixel((piece.width // 2, 0)), 255)
                self.assertEqual(piece.getpixel((piece.width // 2, piece.height - 1)), 255)

        # A legend line just past a page stays with its chart rather than becoming its own piece.
        chart = Image.new('RGB', (1000, 1400), 'white')
        chart.paste((40, 40, 40), (100, 40, 900, 1250))
        chart.paste((40, 40, 40), (100, 1280, 300, 1300))
        output = BytesIO()
        chart.save(output, 'PNG')
        self.assertEqual(len(graphic_parts(output.getvalue())), 1)


MATHJAX = '<script>window.MathJax = {tex: {inlineMath: [["\\\\(", "\\\\)"]]}};</script><script src="/mathjax/tex-svg.js"></script>'
FORMULAS = (r'<p>Multiply the rate by \(\sqrt{2}\) when \(\log x\) grows. The law is \[LR^* \times WD^* = 17.04\]'
            r' and $$a = b$$ holds; the price is $5 and <code>\(x\)</code> stays code.</p>')


class FormulaTests(unittest.TestCase):
    def page(self, head, body):
        return (f'<html lang="en"><head><title>Scaling laws</title>{head}</head><body><article><h1>Scaling laws</h1>'
                f'{PROSE}{body}</article></body></html>').encode()

    def test_tex_on_mathjax_pages_becomes_mathml(self):
        article = build_article(self.page(MATHJAX, FORMULAS), 'https://example.org/laws', lambda *a, **k: 1 / 0)
        tree = ET.fromstring(f'<div xmlns:m="{MATHML}">{article.section.html}</div>')
        formulas = tree.findall(f'.//{{{MATHML}}}math')
        self.assertEqual([f.get('display') for f in formulas], ['inline', 'inline', 'block', 'block'])
        self.assertTrue(formulas[0].findall(f'.//{{{MATHML}}}msqrt'))
        self.assertIn('mspace', ET.tostring(formulas[1]).decode())  # "log x", as TeX spaces it
        text = ''.join(tree.itertext())
        self.assertNotIn(chr(92) + '[', text)
        self.assertIn('the price is $5', text)
        self.assertIn(chr(92) + '(x' + chr(92) + ')', text)  # code is not typeset
        with zipfile.ZipFile(BytesIO(article.epub())) as archive:
            self.assertIn('properties="mathml"', archive.read('OEBPS/content.opf').decode())

    def test_pages_without_a_tex_renderer_keep_their_text(self):
        article = build_article(self.page('', FORMULAS), 'https://example.org/laws', lambda *a, **k: 1 / 0)
        self.assertNotIn('<math', article.section.html)
        self.assertIn(chr(92) + 'sqrt{2}', article.section.html)

    def test_page_mathml_is_kept_and_sanitized(self):
        formula = ('<math><semantics><mrow><mi onclick="bad()" href="javascript:bad()">x</mi><msup><mi>y</mi><mn>2</mn></msup>'
                   '</mrow><annotation encoding="application/x-tex">x y^2</annotation></semantics></math>')
        article = build_article(self.page('', f'<p>Area {formula} grows.</p>'), 'https://example.org/laws', lambda *a, **k: 1 / 0)
        markup = article.section.html
        self.assertIn(f'<math xmlns="{MATHML}">', markup)
        self.assertIn('<msup><mi>y</mi><mn>2</mn></msup>', markup)
        for unwanted in ('annotation', 'onclick', 'javascript', 'x y^2'):
            self.assertNotIn(unwanted, markup)
