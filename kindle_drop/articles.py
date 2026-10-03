"""Preserve article structure and package bounded, offline images for EPUB readers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import hashlib
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
from PIL import Image, ImageOps
import trafilatura

MAX_IMAGES = 32
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


def image_source(node, url):
    srcset = node.get('data-srcset') or node.get('srcset') or ''
    if not srcset and node.getparent() is not None and node.getparent().tag == 'picture':
        sources = node.getparent().findall('source')
        srcset = next((s.get('srcset') for s in sources if s.get('srcset')), '')
    choices = []
    for item in srcset.split(','):
        parts = item.strip().split()
        if len(parts) == 2 and re.fullmatch(r'\d+(?:\.\d+)?[wx]', parts[1]):
            choices.append((float(parts[1][:-1]), parts[0]))
    if choices:
        # Choose a readable 1x/wide variant, avoiding giant 2x/4x downloads.
        wide = any(item.strip().endswith('w') for item in srcset.split(','))
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


def normalize_image(raw):
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds 8 MB.')
    with Image.open(BytesIO(raw)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('Image dimensions are too large.')
        image = ImageOps.exif_transpose(image)
        image.thumbnail((1600, 2400), Image.Resampling.LANCZOS)
        if image.mode not in ('RGB', 'RGBA', 'L', 'LA'):
            image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')
        buffer = BytesIO()
        # PNG keeps screenshot text crisp and works reliably in EPUB readers.
        image.save(buffer, format='PNG', optimize=True)
        return buffer.getvalue()


def download_image(source, fetch):
    if source.startswith('data:'):
        if not re.match(r'data:image/(png|jpeg|gif|webp);base64,', source, re.I):
            raise ValueError('Unsupported embedded image.')
        encoded = source.partition(',')[2]
        if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise ValueError('Embedded image exceeds 8 MB.')
        raw = base64.b64decode(encoded, validate=True)
    else:
        raw, _, _ = fetch(source, max_bytes=MAX_IMAGE_BYTES, timeout=12)
    return raw, normalize_image(raw)


def build_article(raw, url, folder, fetch):
    body, source_tree = content_tree(raw, url)
    metadata = trafilatura.extract_metadata(source_tree, default_url=url)
    title = (metadata.title if metadata else None) or source_tree.findtext('.//h1') or urlsplit(url).hostname or 'Article'
    author = (metadata.author if metadata else None) or urlsplit(url).hostname or 'Web'
    title, author = str(title).strip(), str(author).strip()
    for heading in list(body.iter('h1')):
        if heading.text_content().strip() == title:
            heading.drop_tree()
        else:
            heading.tag = 'h2'
    nodes = list(body.iter('img'))
    sources = list(dict.fromkeys(n.get('src', '') for n in nodes))
    warnings, assets, total = [], {}, 0
    selected = sources[:MAX_IMAGES]
    # Small bounded batches cap in-flight downloads and memory, even with oversized originals.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for start in range(0, len(selected), 4):
            batch = selected[start:start + 4]
            futures = [pool.submit(download_image, src, fetch) for src in batch]
            for src, future in zip(batch, futures):
                try:
                    original, normalized = future.result()
                    total += max(len(original), len(normalized))
                    if total > MAX_TOTAL_IMAGES:
                        raise ValueError('The total article image limit is 32 MB.')
                    name = 'images/' + hashlib.sha256(normalized).hexdigest()[:20] + '.png'
                    target = Path(folder) / name
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(normalized)
                    assets[src] = name
                except Exception:
                    warnings.append('An article image could not be embedded. Its original link is included.')
            if total > MAX_TOTAL_IMAGES:
                break
    for node in nodes:
        src = node.get('src', '')
        if src in assets:
            node.set('src', assets[src])
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
    document = html.Element('html', lang=source_tree.get('lang', 'en'))
    head = etree.SubElement(document, 'head')
    etree.SubElement(head, 'meta', charset='utf-8')
    etree.SubElement(head, 'title').text = title
    etree.SubElement(head, 'style').text = CSS
    output = etree.SubElement(document, 'body')
    etree.SubElement(output, 'h1').text = title
    etree.SubElement(output, 'p', attrib={'class': 'byline'}).text = author
    output.append(body)
    attribution = etree.SubElement(output, 'p', attrib={'class': 'source'})
    attribution.text = 'Source: '
    etree.SubElement(attribution, 'a', href=url).text = url
    text = '<!DOCTYPE html>\n' + html.tostring(document, encoding='unicode', method='html')
    return text, title, author, len(set(assets.values())), list(dict.fromkeys(warnings))
