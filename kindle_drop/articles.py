"""Preserve article structure and package bounded, offline images as an EPUB for e-readers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import base64
import hashlib
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import eink, language
from .epub import Section, epub_bytes

MAX_IMAGES = 32
IMAGE_WORKERS = 6
COVER_SIZE = (1072, 1448)  # a 300 ppi Kindle screen
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
    return src if src.startswith('data:') else urljoin(url, src)


def content_tree(raw, url):
    parser = html.HTMLParser(no_network=True)
    tree = html.fromstring(raw, parser=parser)
    # Prefer semantic article bodies: heuristic text extraction can discard pre/code and captions.
    candidates = tree.xpath('//*[@itemprop="articleBody"] | //article')
    candidates = [n for n in candidates if len(n.text_content().strip()) >= 120]
    if candidates:
        body = deepcopy(max(candidates, key=lambda n: len(n.text_content())))
        body.tag = 'div'
    else:
        import trafilatura  # ~1 s to import; only needed for articles
        extracted = trafilatura.extract(raw, url=url, output_format='html', include_comments=False,
                                        include_tables=True, include_formatting=True,
                                        include_links=True, include_images=True)
        if not extracted:
            raise ValueError('Could not extract the article. It may require login or JavaScript.')
        extracted_tree = html.fromstring(extracted, parser=parser)
        body = extracted_tree.find('body')
        if body is None:
            body = extracted_tree
        body.tag = 'div'
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
    for node in list(body.iterdescendants()):
        if not isinstance(node.tag, str):
            if node.getparent() is not None:
                node.getparent().remove(node)
            continue
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
    return body, tree


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
    data, extension = eink.save(image)
    if lossless and extension == 'jpg':
        # Screenshots keep crisp text as PNG unless that costs much more than a JPEG.
        crisp = eink.png(image)
        if len(crisp) <= 1.5 * len(data):
            data, extension = crisp, 'png'
    return data, extension, image.size


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


def cover_image(title, site, picture=None):
    """A portrait cover as JPEG: the article's first image above its title, and the site."""
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
    label = font(34, bold=False)
    footer = height - margin - 40
    draw.line((margin, footer - 28, width - margin, footer - 28), fill=0, width=3)
    draw.text((margin, footer), site, font=label, fill=70)
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


def build_article(raw, url, fetch):
    import trafilatura
    body, source_tree = content_tree(raw, url)
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
    warnings, assets, names, total, picture = [], {}, {}, 0, None
    selected = sources[:MAX_IMAGES]
    # All downloads start at once on a few workers (each holds at most one original);
    # results are taken in document order, so the first suitable image is the cover.
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
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
    for node in nodes:
        src = node.get('src', '')
        if src in names:
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
    if picture is None and (preview := social_image(source_tree, url)):
        try:  # an article without a usable image of its own: its preview card, cover only
            _, (data, _, size) = download_image(preview, fetch)
            picture = data if cover_candidate(size) else None
        except Exception:
            pass
    try:
        cover = cover_image(title, site, Image.open(BytesIO(picture)) if picture else None)
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
