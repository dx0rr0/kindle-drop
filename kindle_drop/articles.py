"""Preserve article structure and package bounded, offline images as an EPUB for e-readers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import base64
import hashlib
from io import BytesIO
import re
from urllib.parse import quote, urljoin, urlsplit

from lxml import etree, html
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import eink, formulas, language
from .epub import Section, epub_bytes

MAX_IMAGES = 32
IMAGE_WORKERS = 6
COVER_SIZE = (1072, 1448)  # a 300 ppi Kindle screen
COVER_LABEL = 'ARTÍCULO'  # tells an article from a book in history, search and collections
XML_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')
LANGUAGE = re.compile(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*')
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGES = 32 * 1024 * 1024
MAX_PIXELS = 32_000_000
ALLOWED = {'div', 'section', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
           'strong', 'b', 'em', 'i', 'u', 's', 'del', 'sub', 'sup', 'br', 'hr', 'a',
           'blockquote', 'pre', 'code', 'figure', 'figcaption', 'img', 'table', 'thead',
           'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'dl', 'dt', 'dd'}
REMOVE = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'button', 'input', 'form',
          'select', 'textarea', 'nav', 'aside', 'footer', 'header', 'noscript', 'template',
          'video', 'audio', 'canvas'}
# Charts the page draws itself are rendered by a local browser (see browser.py).
GRAPHIC = 'kd-graphic:'  # placeholder image source; the .png suffix keeps it through text extraction
MAX_GRAPHICS = 16
GRAPHIC_TEXT = 1500  # characters: a chart's labels, not a whole section of the article
LABEL_TEXT = 300  # characters a containing block may add to a chart (legend, title, axis labels)
GEOMETRY = re.compile(r'(?:^|;)\s*(?:width|height|transform|flex-basis)\s*:', re.I)
FIGURE_LABEL = re.compile(r'^\s*(?:figure|fig\.|table|chart|diagram)\s*\d+[a-z]?\s*[:.–—-]', re.I)
HIDDEN = ([], None)  # a rendered graphic the page does not display
INK = 0.85  # darkens light tones of rendered charts: pastel colors are near-white on e-ink
PAGE_RATIO = 1.3  # height / width of a rendered chart piece that fits one e-reader page
SLIVER_ALLOWANCE = 1.25  # a chart piece may grow this much past a page instead of leaving a sliver
MAX_STYLESHEETS = 40  # long illustrated articles ship a stylesheet per figure
MAX_STYLESHEET_BYTES = 2 * 1024 * 1024
MAX_STYLES_TOTAL = 8 * 1024 * 1024

CSS = '''
body { font-family: serif; line-height: 1.5; color: #111; }
h1, h2, h3, h4, h5, h6 { font-family: sans-serif; text-align: left;
  line-height: 1.25; page-break-after: avoid; break-after: avoid; }
h1 { font-size: 1.8em; margin: 0 0 .7em; }
h2 { font-size: 1.4em; margin: 1.5em 0 .6em; }
h3 { font-size: 1.2em; margin: 1.2em 0 .5em; }
h4, h5, h6 { font-size: 1.05em; margin: 1em 0 .4em; }
p { margin: 0 0 .85em; text-indent: 0; orphans: 2; widows: 2; }
.byline, .source, figcaption { font-family: sans-serif; font-size: .85em; color: #444; }
.byline { margin-bottom: 1.4em; }
.source { margin-top: 2em; border-top: 1px solid #999; padding-top: .7em; overflow-wrap: anywhere; }
ul, ol { margin: .5em 0 1em; padding-left: 1.5em; }
li { margin-bottom: .4em; } li p { margin-bottom: .3em; }
a { color: inherit; text-decoration: underline; }
img { max-width: 100%; height: auto; }
figure { margin: 1.2em 0; text-align: center; }
figcaption { margin-top: .4em; line-height: 1.35; text-align: left; }
blockquote { margin: 1em 0 1em .6em; padding-left: .8em; border-left: 2px solid #999; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8em; line-height: 1.35;
  margin: 1em 0; padding: .7em; border: 1px solid #bbb; background: #f4f4f4; }
code { font-family: monospace; font-size: .9em; overflow-wrap: anywhere; }
pre code { font-size: 1em; }
table { border-collapse: collapse; font-size: .85em; width: 100%; margin: 1em 0; }
th, td { padding: .4em; border: 1px solid #999; text-align: left; }
hr { margin: 1.4em 0; border: 0; border-top: 1px solid #999; }
math[display="block"] { display: block; margin: .8em 0; text-align: center; }
'''


def srcset_candidates(srcset):
    """(url, descriptor) pairs, following the HTML parsing rules.

    URLs may contain commas (image CDNs such as Substack's use them for resize options);
    a candidate ends at whitespace, and its descriptor at the next comma.
    """
    candidates, position = [], 0
    while position < len(srcset):
        while position < len(srcset) and (srcset[position].isspace() or srcset[position] == ','):
            position += 1
        start = position
        while position < len(srcset) and not srcset[position].isspace():
            position += 1
        url = srcset[start:position]
        if not url:
            break
        if url.endswith(','):
            candidates.append((url.rstrip(','), ''))
            continue
        end = srcset.find(',', position)
        end = len(srcset) if end < 0 else end
        candidates.append((url, srcset[position:end].strip()))
        position = end + 1
    return candidates


def image_source(node, url):
    srcset = node.get('data-srcset') or node.get('srcset') or ''
    if not srcset and node.getparent() is not None and node.getparent().tag == 'picture':
        sources = node.getparent().findall('source')
        srcset = next((s.get('srcset') for s in sources if s.get('srcset')), '')
    candidates = srcset_candidates(srcset)
    choices = [(float(d[:-1]), u) for u, d in candidates if re.fullmatch(r'\d+(?:\.\d+)?[wx]', d)]
    if choices:
        # Choose a readable 1x/wide variant, avoiding giant 2x/4x downloads.
        wide = any(d.endswith('w') for _, d in candidates)
        eligible = [p for p in choices if p[0] <= (1600 if wide else 1)]
        src = max(eligible)[1] if eligible else min(choices)[1]
    else:
        src = node.get('data-src') or node.get('data-original') or node.get('src') or ''
    # Spaces and non-ASCII characters (a "5.04 PM" screenshot name) are percent-encoded for the request.
    return src if src.startswith('data:') else quote(urljoin(url, src), safe="%/:=&?~#+!$,;'@()*[]")


@dataclass
class Graphic:
    """A chart the page draws itself: a standalone document (a scripted widget) or, with
    markup None, the page element marked data-kd-graphic=number."""
    number: int
    markup: str | None
    alt: str


def embedded_document(node):
    """The standalone HTML of an iframe srcdoc or a base64 widget template, if any."""
    if node.tag == 'iframe':
        return node.get('srcdoc') or None
    try:
        return base64.b64decode(''.join(node.itertext()).strip(), validate=True).decode('utf-8')
    except ValueError:
        return None


def squashed(node):
    return ' '.join(node.text_content().split())


def figure_label(root):
    """A "Figure 3: ..." caption inside a graphic, used as its alternative text."""
    for node in root.iter():
        if isinstance(node.tag, str):
            text = squashed(node)
            if FIGURE_LABEL.match(text) and len(text) < 700 and not any(
                    isinstance(child.tag, str) and FIGURE_LABEL.match(squashed(child)) for child in node):
                return text
    return ''


def unmark_containers(node):
    """Drop class and id from the containers holding only this node: names like "widget"
    read as boilerplate to text extraction."""
    text = squashed(node)
    for ancestor in node.iterancestors():
        if ancestor.tag in ('body', 'html') or squashed(ancestor) != text:
            break
        ancestor.attrib.pop('class', None)
        ancestor.attrib.pop('id', None)


def find_graphics(tree):
    """Mark the charts a page draws itself and return them as Graphics.

    Scripted widgets come as standalone documents (iframe srcdoc, base64 templates); a widget
    holding only a table becomes ordinary article content instead. In the page itself, an SVG
    with text labels or a block built from empty, sized elements (CSS bar charts) is a chart.
    """
    graphics = []

    def add(node, markup, alt):
        if len(graphics) < MAX_GRAPHICS:
            node.set('data-kd-graphic', str(len(graphics)))
            graphics.append(Graphic(len(graphics), markup, alt or 'Chart'))

    for node in tree.xpath('//template[@data-widget-html] | //iframe[@srcdoc]'):
        markup = embedded_document(node)
        if not markup or not markup.strip() or node.getparent() is None:
            continue
        document = html.document_fromstring(markup)
        drawn = document.xpath('//script | //svg | //canvas | //img')
        for junk in document.xpath('//script | //style'):
            junk.drop_tree()
        body = document.find('body')
        if body is None or not squashed(body):
            continue
        if document.xpath('//table') and not drawn:
            content = html.Element('div')
            content.text, content.tail = body.text, node.tail
            content.extend(list(body))
            node.getparent().replace(node, content)
            unmark_containers(content)
            continue
        add(node, markup, figure_label(body))
    inside = lambda node: any(a.get('data-kd-graphic') is not None or a.tag in ('a', 'button', 'nav', 'header', 'footer')
                              for a in node.iterancestors())
    for svg in tree.iter('svg'):
        if len(svg.findall('.//text')) >= 2 and not inside(svg):
            # A figure holding only the chart draws its title, legend and responsive variants with it.
            figure = next(svg.iterancestors('figure'), None)
            drawings = figure.xpath('.//svg[not(ancestor::svg)]') if figure is not None else []
            if figure is not None and not inside(figure) and not figure.xpath(
                    './/img | .//picture | .//video | .//canvas | .//pre | .//table | .//blockquote | .//p') \
                    and len(squashed(figure)) - sum(len(squashed(d)) for d in drawings) <= LABEL_TEXT:
                described = figure.xpath('.//*[@role="img"]/@aria-label')
                add(figure, None, figure.get('aria-label') or (described[0] if described else None)
                    or svg.get('aria-label') or figure_label(figure))
            else:
                add(svg, None, svg.get('aria-label') or figure_label(svg))
    counts = {}
    for node in tree.iter():
        if not isinstance(node.tag, str) or not GEOMETRY.search(node.get('style', '')) or squashed(node) or inside(node):
            continue
        # Grow to the block holding the bars and their labels, not into the surrounding prose.
        chart = node
        while (parent := chart.getparent()) is not None and parent.tag not in ('body', 'html') \
                and len(squashed(parent)) <= min(GRAPHIC_TEXT, len(squashed(chart)) + LABEL_TEXT):
            chart = parent
        counts[chart] = counts.get(chart, 0) + 1
    # A figure drawn in markup, not an image: many elements and little text (timelines, diagrams).
    for figure in tree.iter('figure'):
        if figure.get('data-kd-graphic') is None and not inside(figure) and len(squashed(figure)) <= GRAPHIC_TEXT and sum(1 for _ in figure.iter()) >= 24 \
                and not figure.xpath('.//img | .//picture | .//video | .//canvas | .//pre | .//table | .//blockquote'):
            counts[figure] = 3
    charts = [chart for chart, bars in counts.items() if bars >= 3 and squashed(chart) and chart.tag not in ('body', 'html')]
    # Sibling charts sharing a legend or title (one per metric, say) are one chart.
    merged = True
    while merged:
        merged = False
        parents = {}
        for chart in charts:
            if chart.getparent() is not None:
                parents.setdefault(chart.getparent(), []).append(chart)
        for parent, children in parents.items():
            text = len(squashed(parent))
            extra = text - sum(len(squashed(child)) for child in children)
            if len(children) >= 2 and parent.tag not in ('body', 'html') and text <= GRAPHIC_TEXT and extra <= LABEL_TEXT:
                charts = [chart for chart in charts if chart not in children] + [parent]
                merged = True
                break
    for chart in charts:
        if not any(other in charts for other in chart.iterancestors()):
            add(chart, None, chart.get('aria-label') or figure_label(chart))
    return graphics


def with_placeholders(tree, graphics):
    """A copy of the page whose graphics are placeholder images, which text extraction keeps."""
    copy = deepcopy(tree)
    for node in copy.xpath('//*[@data-kd-graphic]'):
        number = int(node.get('data-kd-graphic'))
        image = html.Element('img', src=f'{GRAPHIC}{number}.png', alt=graphics[number].alt)
        image.tail = node.tail
        node.getparent().replace(node, image)
        unmark_containers(image)
    return copy


def page_document(tree, url, fetch):
    """The page with its stylesheets inlined and every script and external reference removed."""
    page = deepcopy(tree)
    links = [urljoin(url, node.get('href', '')) for node in page.xpath('//link[@href]')
             if 'stylesheet' in node.get('rel', '').lower().split()]
    links = [link for link in dict.fromkeys(links) if urlsplit(link).scheme in ('http', 'https')][:MAX_STYLESHEETS]

    def stylesheet(link):
        try:
            return fetch(link, max_bytes=MAX_STYLESHEET_BYTES, timeout=12)[0].decode('utf-8', 'replace')
        except Exception:
            return ''  # the chart still renders, with the browser's default styles

    with ThreadPoolExecutor(max_workers=6) as pool:
        sheets, total = [], 0
        for sheet in pool.map(stylesheet, links):
            total += len(sheet)
            if total > MAX_STYLES_TOTAL:
                break
            sheets.append(sheet)
    for node in page.xpath('//script | //noscript | //template | //iframe | //object | //embed | //link | //base'
                           ' | //meta[@http-equiv]'):
        node.drop_tree()
    # Only the charts and their containers (whose classes style them): a short page lays out
    # once, so the measured positions still hold when the browser captures it.
    charts = page.xpath('//body//*[@data-kd-graphic]')
    path = {ancestor for chart in charts for ancestor in chart.iterancestors()}

    def kept(node):
        # Styles anywhere in the page, and SVG definitions (gradients, symbols) charts may reference.
        return node.tag == 'style' or node.tag == 'svg' and len(node) and all(
            isinstance(child.tag, str) and child.tag in ('defs', 'symbol', 'style') for child in node)

    def prune(node):
        for child in list(node):
            if child in charts:
                continue  # whole, with its caption, even when a chart inside it is marked too
            if child in path:
                prune(child)
            elif not kept(child):
                child.drop_tree()

    body = page.find('body')
    if body is not None:
        prune(body)
    head = page.find('head')
    if head is None:
        head = html.Element('head')
        page.insert(0, head)
    style = html.Element('style')
    style.text = '\n'.join(sheets).replace('</', '< /')
    head.insert(0, style)
    return html.tostring(page, encoding='unicode')


def paper_white(image):
    """Map a light page background (cream, light gray) to white paper, and darken the chart's
    light tones so pastel series stay visible in 16 gray levels."""
    edge = [image.getpixel((x, y)) for x in range(0, image.width, max(1, image.width // 32)) for y in (0, image.height - 1)]
    if sorted(edge)[len(edge) // 2] < 128:
        return image  # a chart drawn on a dark page stays as drawn
    histogram = image.histogram()
    paper = max(range(160, 256), key=lambda value: histogram[value])  # the most common light tone
    levels = [min(255, value * 255 / paper) for value in range(256)]
    return image.point([255 if level >= 250 else round(level * INK) for level in levels])


def blank_row(image, y):
    return image.crop((0, y, image.width, y + 1)).getextrema()[0] >= 245


def encode_lossless(image):
    """A prepared image from a lossless source: JPEG, unless crisp PNG costs at most 1.5x."""
    data, extension = eink.save(image)
    if extension == 'jpg':
        crisp = eink.png(image)
        if len(crisp) <= 1.5 * len(data):
            data, extension = crisp, 'png'
    return data, extension, image.size


def graphic_parts(png):
    """A rendered chart as gray images on white, trimmed and cut at blank rows into page-sized pieces."""
    with Image.open(BytesIO(png)) as image:
        image = paper_white(eink.gray(image))
    box = image.point(lambda value: 255 if value < 243 else 0).getbbox()
    if box is None:
        raise ValueError('The chart is blank.')
    pad = 16
    image = image.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                        min(image.width, box[2] + pad), min(image.height, box[3] + pad)))
    pieces, limit = [], max(200, round(image.width * PAGE_RATIO))

    def blank_band(top):
        """(cut, width): the middle of the widest blank band between top/2 and top."""
        cut, run, best = top, 0, 0
        for y in range(top // 2, top + 1):
            run = run + 1 if blank_row(image, y) else 0
            if run and run >= best:
                cut, best = y - run // 2, run
        return cut, best

    while image.height > limit:
        # Cut in the middle of the widest blank band in the lower half of the page: between
        # panels rather than under a panel's title.
        cut, _ = blank_band(limit)
        if image.height - cut < limit // 4 and image.height <= limit * SLIVER_ALLOWANCE:
            break  # a slightly taller piece beats a sliver holding one legend line or an axis
        pieces.append(image.crop((0, 0, image.width, cut)))
        image = image.crop((0, cut, image.width, image.height))
    pieces.append(image)
    pieces = [piece for piece in pieces if piece.point(lambda value: 255 if value < 243 else 0).getbbox()]
    return [encode_lossless(eink.prepare(piece)) for piece in pieces]


def render_graphics(graphics, tree, url, fetch):
    """{number: ([(bytes, extension, size)], caption)} for the graphics a local browser could draw,
    and HIDDEN for those the page does not display (responsive variants)."""
    from .browser import render
    in_page = [graphic for graphic in graphics if graphic.markup is None]
    documents, owners = [], []
    if in_page:
        documents.append((page_document(tree, url, fetch), [f'[data-kd-graphic="{g.number}"]' for g in in_page]))
        owners.append(in_page)
    for graphic in graphics:
        if graphic.markup is not None:
            documents.append((graphic.markup, [None]))
            owners.append([graphic])
    found = [(graphic, shot) for group, shots in zip(owners, render(documents)) for graphic, shot in zip(group, shots)]
    shots = [(graphic, shot) for graphic, shot in found if shot]
    hidden = {graphic.number: HIDDEN for graphic, shot in found if shot is False}

    def pieces(item):
        try:
            return graphic_parts(item[1][0])
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        drawn = {graphic.number: (parts, shot[1]) for (graphic, shot), parts in zip(shots, pool.map(pieces, shots)) if parts}
    return {**hidden, **drawn}


def unwrapped_figures(tree):
    """A copy whose figures read as paragraphs. Outside an <article>, text extraction drops every
    <figure> with its images and caption, but keeps an image in a paragraph and a caption as text."""
    copy = deepcopy(tree)
    # Chart placeholders stand in for a whole figure, so they need the same treatment.
    images = [i for figure in copy.iter('figure') for i in figure.iter('img')]
    images += [i for i in copy.iter('img') if i.get('src', '').startswith(GRAPHIC) and i not in images]
    for image in images:
        if image.getparent().tag != 'p':
            paragraph = html.Element('p')
            image.getparent().replace(image, paragraph)
            paragraph.append(image)
            paragraph.tail, image.tail = image.tail, None
    for caption in copy.iter('figcaption'):
        caption.tag = 'p'
    return copy


def extracted_tree(extracted, parser):
    """Trafilatura's HTML, with the inline code it writes as <pre> (which would end the paragraph) back as <code>."""
    try:
        tree = etree.fromstring(extracted.encode('utf-8'))
    except etree.XMLSyntaxError:
        return html.fromstring(extracted, parser=parser)
    for node in tree.iter('pre'):
        if node.getparent().tag in ('p', 'li', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'a'):
            node.tag = 'code'
    return html.fromstring(etree.tostring(tree, encoding='unicode'), parser=parser)


def located_body(tree, extracted):
    """(element, lead) holding the text trafilatura extracted, or (None, []).

    On a page without an <article>, trafilatura only recovers loose paragraphs, losing headings,
    lists and code blocks; its paragraphs still show which element holds the article, which is
    then kept whole, like an <article>. Lead paragraphs before it (a standfirst) come along."""
    blocks = [' '.join(''.join(p.itertext()).split()) for p in extracted.iter('p')]
    blocks = [text for text in blocks if len(text) >= 40]
    total = sum(map(len, blocks))
    page = {}
    for node in tree.iter('p', 'figcaption'):  # captions read as paragraphs (unwrapped_figures)
        page.setdefault(squashed(node), node)
    found = [(page[text], len(text)) for text in blocks if text in page]
    if not total or sum(weight for _, weight in found) < 0.8 * total:
        return None, []
    weights, depth = {}, {}
    for node, weight in found:
        for level, ancestor in enumerate(reversed(list(node.iterancestors()))):
            weights[ancestor] = weights.get(ancestor, 0) + weight
            depth[ancestor] = level
    holders = [node for node, weight in weights.items() if weight >= 0.9 * total and node.tag not in ('html', 'body')]
    if not holders:
        return None, []
    holder = max(holders, key=depth.get)
    order = {node: index for index, node in enumerate(tree.iter())}
    lead = []
    for node, _ in found:
        if holder in node.iterancestors():
            break
        if order[node] < order[holder] and node not in lead:
            lead.append(node)
    return holder, lead[:2]


def content_tree(raw, url):
    """(article body, page tree, graphics): the body is sanitized and has graphic placeholders."""
    parser = html.HTMLParser(no_network=True)
    page = html.fromstring(raw, parser=parser)
    graphics = find_graphics(page)
    tree = with_placeholders(page, graphics) if graphics else page
    # Prefer semantic article bodies: heuristic text extraction can discard pre/code and captions.
    candidates = tree.xpath('//*[@itemprop="articleBody"] | //article')
    candidates = [n for n in candidates if len(n.text_content().strip()) >= 120]
    lead = []
    if not candidates:
        import trafilatura  # ~1 s to import; only needed for articles
        source = html.tostring(unwrapped_figures(tree), encoding='unicode') if graphics or tree.find('.//figure') is not None else raw
        extracted = trafilatura.extract(source, url=url, output_format='html', include_comments=False,
                                        include_tables=True, include_formatting=True,
                                        include_links=True, include_images=True)
        if not extracted:
            raise ValueError('Could not extract the article. It may require login or JavaScript.')
        extracted = extracted_tree(extracted, parser)
        holder, lead = located_body(tree, extracted)
        if holder is not None:
            candidates = [holder]
        else:
            body = extracted.find('body')
            if body is None:
                body = extracted
            body.tag = 'div'
    if candidates:
        body = deepcopy(max(candidates, key=lambda n: len(n.text_content())))
        body.tag = 'div'
        for index, node in enumerate(lead):
            body.insert(index, deepcopy(node))
            body[index].tail = None
        # A code block's toolbar (language, Copy button) is not part of the article.
        for node in list(body.iter('pre')):
            bar = node.getprevious()
            if bar is not None and bar.find('.//button') is not None and len(squashed(bar)) <= 40:
                bar.drop_tree()
    # Parts a stylesheet sets apart ("FIG 2", then the title; "23.6 ms" over "±0.63") keep a space between them.
    for holder in body.iter('figcaption', 'td', 'th'):
        for part in holder.iterdescendants('span', 'div', 'p', 'b', 'strong', 'small'):
            if part.getnext() is not None and not part.tail:
                part.tail = ' '
    # Select image URLs while responsive/lazy attributes are still available.
    for node in list(body.iter('img')):
        classes = node.get('class', '')
        hidden = node.get('aria-hidden') == 'true' or node.get('hidden') is not None
        hidden = hidden or 'hidden' in classes.split() or 'display:none' in node.get('style', '').replace(' ', '')
        # Prefer variants intended for a light page, particularly for charts on e-ink.
        hidden = hidden or 'light-theme' in classes and 'hidden' in classes
        if hidden:
            node.drop_tree()
        else:
            node.set('src', image_source(node, url))
    # The page's own MathML (KaTeX includes it beside its HTML rendering) stays typeset.
    for node in list(body.iter('math')):
        if node.getparent() is not None:
            formula = formulas.mathml(node)
            formula.tail = node.tail
            node.getparent().replace(node, formula)
    for node in list(body.iterdescendants()):
        if not isinstance(node.tag, str):
            if node.getparent() is not None:
                # Keep the text after a comment: React writes "@<!-- -->ClaudeDevs".
                previous, parent = node.getprevious(), node.getparent()
                if previous is not None:
                    previous.tail = (previous.tail or '') + (node.tail or '')
                else:
                    parent.text = (parent.text or '') + (node.tail or '')
                parent.remove(node)
            continue
        if node.tag.startswith('{'):
            continue  # sanitized MathML
        tag = node.tag.lower()
        if tag in REMOVE or node.get('aria-hidden') == 'true' or node.get('hidden') is not None:
            node.drop_tree()
            continue
        if node.getparent() is None:
            continue
        if tag not in ALLOWED:
            node.drop_tag()
            continue
        attributes = {}
        if tag == 'img':
            attributes = {k: node.get(k) for k in ('src', 'alt') if node.get(k)}
        elif tag == 'a':
            target = urljoin(url, node.get('href', ''))
            if urlsplit(target).scheme in ('http', 'https'):
                attributes['href'] = target
        if tag in ('td', 'th'):
            for key in ('colspan', 'rowspan'):
                value = node.get(key, '')
                if value.isdigit() and 1 <= int(value) <= 30:
                    attributes[key] = value
        identifier = node.get('id', '')
        if re.fullmatch(r'[A-Za-z][\w.-]{0,100}', identifier):
            attributes['id'] = identifier
        node.attrib.clear()
        node.attrib.update(attributes)
    body.attrib.clear()
    if len(body.text_content().strip()) < 120:
        raise ValueError('The page has too little readable text. Try the full article URL.')
    return body, page, graphics


@dataclass
class Article:
    title: str
    author: str
    language: str
    section: Section
    assets: dict[str, bytes]  # images/... referenced by the section
    cover: bytes | None  # JPEG
    warnings: list[str] = field(default_factory=list)

    @property
    def images(self):
        return len(self.assets)

    def epub(self):
        identifier = 'urn:sha256:' + hashlib.sha256(self.section.html.encode()).hexdigest()
        # The cover shows in library views; reading starts at the article's own title.
        return epub_bytes([self.section], self.title, self.author, self.language, identifier,
                          self.assets, CSS, self.cover, cover_page=False)


LOSSLESS = {'PNG', 'GIF', 'BMP', 'TIFF'}


def normalize_image(raw):
    """Return (bytes, extension, (width, height)): gray, screen sized, JPEG or PNG."""
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds 8 MB.')
    with Image.open(BytesIO(raw)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('Image dimensions are too large.')
        lossless = image.format in LOSSLESS
        image.draft('L', (max(eink.MAX_SIZE),) * 2)  # EXIF rotation below may swap the sides
        image = eink.prepare(ImageOps.exif_transpose(image))
    # Screenshots keep crisp text as PNG unless that costs much more than a JPEG.
    return encode_lossless(image) if lossless else (*eink.save(image), image.size)


def download_image(source, fetch):
    """Return (original byte count, normalize_image result)."""
    if source.startswith('data:'):
        if not re.match(r'data:image/(png|jpeg|gif|webp);base64,', source, re.I):
            raise ValueError('Unsupported embedded image.')
        encoded = source.partition(',')[2]
        if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise ValueError('Embedded image exceeds 8 MB.')
        raw = base64.b64decode(encoded, validate=True)
    else:
        raw, _, _ = fetch(source, max_bytes=MAX_IMAGE_BYTES, timeout=12)
    return len(raw), normalize_image(raw)


def cover_candidate(size):
    """Large enough to carry a cover, and not a thin banner or divider."""
    width, height = size
    return width >= 300 and height >= 150 and width <= 4 * height and height <= 3 * width


def social_image(tree, url):
    """The page's own preview image (Open Graph / Twitter card), if declared."""
    for path in ('//meta[@property="og:image"]/@content', '//meta[@name="og:image"]/@content',
                 '//meta[@name="twitter:image"]/@content', '//meta[@property="twitter:image"]/@content'):
        found = [value.strip() for value in tree.xpath(path) if value.strip()]
        if found:
            target = urljoin(url, found[0])
            return target if urlsplit(target).scheme in ('http', 'https') else None
    return None


FONTS = {True: ('georgiab.ttf', 'DejaVuSerif-Bold.ttf', 'LiberationSerif-Bold.ttf', 'NotoSerif-Bold.ttf', 'Georgia Bold.ttf'),
         False: ('segoeui.ttf', 'DejaVuSans.ttf', 'LiberationSans-Regular.ttf', 'NotoSans-Regular.ttf', 'Arial.ttf')}
FONT_PATHS = {}


def font(size, bold=True):
    """A system serif (titles) or sans (labels), falling back to Pillow's bundled font."""
    if bold not in FONT_PATHS:
        FONT_PATHS[bold] = None
        for name in FONTS[bold]:
            try:
                FONT_PATHS[bold] = ImageFont.truetype(name, 10).path
                break
            except OSError:
                continue
    path = FONT_PATHS[bold]
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def wrap(draw, text, face, width):
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}'.strip()
        if line and draw.textlength(trial, font=face) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


def cover_image(title, site, picture=None, label=None):
    """A portrait cover as JPEG: the article's first image above its title, and the site.
    A label sits in a black tab over the image's top corner, large enough to read in the mosaic."""
    width, height = COVER_SIZE
    margin = 72
    page = Image.new('L', COVER_SIZE, 255)
    draw = ImageDraw.Draw(page)
    top = 220  # a text-only cover starts its title lower
    if picture is not None:
        picture = eink.gray(picture)
        scale = min(width / picture.width, 0.56 * height / picture.height)
        picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
                                 Image.Resampling.LANCZOS)
        page.paste(picture, ((width - picture.width) // 2, 0))
        top = picture.height + 64
    small = font(34, bold=False)
    footer = height - margin - 40
    draw.line((margin, footer - 28, width - margin, footer - 28), fill=0, width=3)
    draw.text((margin, footer), site, font=small, fill=70)
    room = footer - 72 - top
    for size in (88, 80, 72, 64, 56, 50, 44, 40):
        face, leading = font(size), round(size * 1.2)
        lines = wrap(draw, title, face, width - 2 * margin)
        if len(lines) * leading <= room:
            break
    if len(lines) * leading > room:
        lines = lines[:max(1, room // leading)]
        lines[-1] += '…'
    for number, line in enumerate(lines):
        draw.text((margin, top + number * leading), line, font=face, fill=0)
    if label:
        # The library mosaic shows a cover at about a quarter of its size: 52 px stays legible.
        tab, pad, y = font(52), 28, 48
        right = margin + draw.textlength(label, font=tab) + pad
        draw.rounded_rectangle((margin - pad, y, right, y + 96), radius=18, fill=0, outline=255, width=6)
        draw.text((margin, y + 18), label, font=tab, fill=255)
    buffer = BytesIO()
    page.save(buffer, 'JPEG', quality=85)
    return buffer.getvalue()


def section(output, body, title):
    """Serialize the article as XHTML with unique ids and a heading outline for the TOC."""
    seen = set()
    for node in output.iter():
        identifier = node.get('id')
        if identifier in seen:
            del node.attrib['id']
        elif identifier:
            seen.add(identifier)
    headings = [(1, title, 'kd-title')]
    for number, node in enumerate(body.iter('h2', 'h3')):
        text = ' '.join(node.text_content().split())
        if not text:
            continue
        if not node.get('id'):
            node.set('id', f'kd-h{number}')
        headings.append((int(node.tag[1]), text, node.get('id')))
    markup = etree.tostring(output, method='xml', encoding='unicode')
    # As Calibre did: e-reader fonts often lack U+2011, and soft hyphens are left to the reader.
    markup = markup.replace('\u2011', '-').replace('\xad', '')
    return Section(XML_INVALID.sub('', markup), headings)


def clean_title(title, site, site_name=None):
    """Drop heading permalink marks and a trailing " | Site" or " - Site" the cover already shows."""
    title = title.strip().rstrip('¶§#🔗').strip()
    squash = lambda text: re.sub(r'[^0-9a-z]', '', text.lower())
    names = {squash(site), squash(site.rpartition('.')[0]), squash(site_name or '')} - {''}
    for separator in (' | ', ' - ', ' – ', ' — ', ' · '):
        head, found, tail = title.rpartition(separator)
        tail = squash(tail)
        if found and head.strip() and any(tail == name or (len(tail) >= 4 and tail in name) for name in names):
            return head.strip()
    return title


def build_article(raw, url, fetch, article=True):
    """article: label the cover as an article; False packages the page as a book."""
    import trafilatura
    body, source_tree, graphics = content_tree(raw, url)
    dollars = formulas.renderer(source_tree)
    if dollars is not None:
        formulas.typeset(body, dollars)
    metadata = trafilatura.extract_metadata(source_tree, default_url=url)
    site = (urlsplit(url).hostname or '').removeprefix('www.')
    title = (metadata.title if metadata else None) or source_tree.findtext('.//h1') or urlsplit(url).hostname or 'Article'
    author = (metadata.author if metadata else None) or urlsplit(url).hostname or 'Web'
    site_name = metadata.sitename if metadata else None
    stated, title = str(title).strip(), clean_title(str(title), site, site_name) or str(title).strip()
    author = str(author).strip()
    for heading in list(body.iter('h1')):
        text = heading.text_content().strip()
        if text in (stated, title) or clean_title(text, site, site_name) == title:
            heading.drop_tree()
        else:
            heading.tag = 'h2'
    nodes = list(body.iter('img'))
    sources = list(dict.fromkeys(n.get('src', '') for n in nodes))
    placed = [graphics[int(src[len(GRAPHIC):-len('.png')])] for src in sources if src.startswith(GRAPHIC)]
    sources = [src for src in sources if not src.startswith(GRAPHIC)]
    warnings, assets, names, total, picture, drawn = [], {}, {}, 0, None, {}
    selected = sources[:MAX_IMAGES]
    # All downloads start at once on a few workers (each holds at most one original);
    # results are taken in document order, so the first suitable image is the cover.
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        # Charts render in a local browser meanwhile; they are never the cover.
        rendering = pool.submit(render_graphics, placed, source_tree, url, fetch) if placed else None
        futures = [pool.submit(download_image, src, fetch) for src in selected]
        for src, future in zip(selected, futures):
            if total > MAX_TOTAL_IMAGES:
                future.cancel()
                continue
            try:
                original, (data, extension, size) = future.result()
                total += max(original, len(data))
                if total > MAX_TOTAL_IMAGES:
                    raise ValueError('The total article image limit is 32 MB.')
                name = f'images/{hashlib.sha256(data).hexdigest()[:20]}.{extension}'
                assets[name], names[src] = data, name
                if picture is None and cover_candidate(size):
                    picture = data
            except Exception:
                warnings.append('An article image could not be embedded. Its original link is included.')
        if rendering is not None:
            try:
                drawn = rendering.result()
            except Exception:
                drawn = {}
    if placed and len(drawn) < len(placed):
        from .browser import find_browser
        warnings.append('Install Microsoft Edge or Google Chrome to include the charts this page draws; their captions are included.'
                        if find_browser() is None else 'A chart could not be drawn; its caption is included.')
    for node in nodes:
        src = node.get('src', '')
        if src.startswith(GRAPHIC):
            graphic = graphics[int(src[len(GRAPHIC):-len('.png')])]
            if drawn.get(graphic.number) is HIDDEN:
                parent = node.getparent()
                previous = node.getprevious()
                if previous is not None:
                    previous.tail = (previous.tail or '') + (node.tail or '')
                else:
                    parent.text = (parent.text or '') + (node.tail or '')
                parent.remove(node)
                continue
            pieces, caption = drawn.get(graphic.number, ([], ''))
            figure = html.Element('figure')
            for data, extension, _ in pieces:
                name = f'images/{hashlib.sha256(data).hexdigest()[:20]}.{extension}'
                assets[name] = data
                etree.SubElement(figure, 'img', src=name, alt=graphic.alt)
            if caption or not pieces:
                label = etree.SubElement(figure, 'figcaption')
                label.text = caption if pieces else f'[Chart: {graphic.alt}] '
                if not pieces:
                    etree.SubElement(label, 'a', href=url).text = 'See it on the original page.'
            figure.tail = node.tail
            node.getparent().replace(node, figure)
        elif src in names:
            node.set('src', names[src])
        else:
            alt = node.get('alt', 'Article image')
            node.tag = 'a'
            node.attrib.clear()
            if urlsplit(src).scheme in ('http', 'https'):
                node.set('href', src)
            node.text = '[Image: ' + alt + ']'
    if len(sources) > MAX_IMAGES or total > MAX_TOTAL_IMAGES:
        warnings.append('Some images exceeded the article image limits; original links are included.')
    for node in list(body.iter('img')):
        if node.getparent() is not None and node.getparent().tag not in ('figure', 'a'):
            parent = node.getparent()
            wrapper = etree.Element('figure')
            parent.replace(node, wrapper)
            wrapper.append(node)
            wrapper.tail, node.tail = node.tail, None
    # A figure is a block of its own: drop a paragraph left around it with no text of its own.
    for node in list(body.iter('p')):
        if len(node) == 1 and node[0].tag == 'figure' and not (node.text or '').strip() and not (node[0].tail or '').strip():
            node.drop_tag()
    if picture is None and (preview := social_image(source_tree, url)):
        try:  # an article without a usable image of its own: its preview card, cover only
            _, (data, _, size) = download_image(preview, fetch)
            picture = data if cover_candidate(size) else None
        except Exception:
            pass
    try:
        label = COVER_LABEL if article else None
        cover = cover_image(title, site, Image.open(BytesIO(picture)) if picture else None, label)
    except Exception:
        cover = None  # a missing cover never blocks the article
    lang = source_tree.get('lang', '').strip()
    if not LANGUAGE.fullmatch(lang):
        lang = language.guess(body.text_content())
    output = html.Element('div')
    etree.SubElement(output, 'h1', id='kd-title').text = title
    etree.SubElement(output, 'p', attrib={'class': 'byline'}).text = author
    output.append(body)
    attribution = etree.SubElement(output, 'p', attrib={'class': 'source'})
    attribution.text = 'Source: '
    etree.SubElement(attribution, 'a', href=url).text = url
    return Article(title, author, lang, section(output, body, title), assets, cover, list(dict.fromkeys(warnings)))
