"""Package rendered chapters as a deterministic EPUB 3 (with an EPUB 2 NCX for older readers).

Writing the container directly takes well under a second, where a Calibre round trip
re-parses the whole book; identical PDFs produce byte-identical EPUBs.
"""
from __future__ import annotations

from html import escape
from io import BytesIO
import mimetypes
import zipfile

from .render import CSS, xhtml

STAMP = (2026, 1, 1, 0, 0, 0)  # fixed ZIP timestamps keep output reproducible
MEDIA = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif'}


def toc_tree(entries):
    """Nest (level, text, href) entries; a deeper level always hangs under the previous entry."""
    root = {'children': []}
    stack = [(0, root)]
    for level, text, href in entries:
        node = {'text': text, 'href': href, 'children': []}
        while stack[-1][0] >= level:
            stack.pop()
        stack[-1][1]['children'].append(node)
        stack.append((level, node))
    return root['children']


def nav_list(nodes):
    items = ''.join(f'<li><a href="{escape(n["href"])}">{escape(n["text"])}</a>'
                    + (nav_list(n['children']) if n['children'] else '') + '</li>' for n in nodes)
    return f'<ol>{items}</ol>'


def ncx_points(nodes, counter):
    output = []
    for node in nodes:
        counter[0] += 1
        output.append(f'<navPoint id="p{counter[0]}" playOrder="{counter[0]}"><navLabel><text>{escape(node["text"])}</text>'
                      f'</navLabel><content src="{escape(node["href"])}"/>{ncx_points(node["children"], counter)}</navPoint>')
    return ''.join(output)


def epub_bytes(chapters, title, author, language, identifier, assets, cover=None):
    """chapters: render.Chapter list; assets: {'images/...': bytes} they reference; cover: JPEG."""
    files = []  # (name in OEBPS, bytes, media type, properties)
    spine = []
    if cover:
        files.append(('cover.jpg', cover, 'image/jpeg', 'cover-image'))
        page = xhtml(title, '<div class="cover"><img src="cover.jpg" alt=""/></div>', language, 'cover')
        files.append(('cover.xhtml', page.encode(), 'application/xhtml+xml', ''))
        spine.append('cover.xhtml')
    entries = []
    for number, chapter in enumerate(chapters, 1):
        name = f'chapter-{number:03}.xhtml'
        heading = next((text for _, text, _ in chapter.headings), title)
        files.append((name, xhtml(heading, chapter.html, language).encode(), 'application/xhtml+xml', ''))
        spine.append(name)
        entries.extend((level, text, f'{name}#{anchor}') for level, text, anchor in chapter.headings)
    if not entries:
        entries = [(1, title, spine[-1] if spine else 'cover.xhtml')]
    tree = toc_tree(entries)
    nav = xhtml('Contents', f'<nav epub:type="toc" id="toc"><h1>Contents</h1>{nav_list(tree)}</nav>', language)
    files.append(('nav.xhtml', nav.encode(), 'application/xhtml+xml', 'nav'))
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{escape(identifier)}"/></head><docTitle><text>{escape(title)}</text></docTitle>'
           f'<navMap>{ncx_points(tree, [0])}</navMap></ncx>')
    files.append(('toc.ncx', ncx.encode(), 'application/x-dtbncx+xml', ''))
    files.append(('style.css', CSS.encode(), 'text/css', ''))
    for name in sorted(assets):
        suffix = name[name.rfind('.'):].lower()
        media = MEDIA.get(suffix) or mimetypes.guess_type(name)[0] or 'application/octet-stream'
        files.append((name, assets[name], media, ''))

    ids = {name: f'f{index}' for index, (name, *_rest) in enumerate(files)}
    manifest = ''.join(f'<item id="{ids[name]}" href="{escape(name)}" media-type="{media}"'
                       + (f' properties="{props}"' if props else '') + '/>' for name, _, media, props in files)
    itemrefs = ''.join(f'<itemref idref="{ids[name]}"/>' for name in spine)
    cover_meta = f'<meta name="cover" content="{ids["cover.jpg"]}"/>' if 'cover.jpg' in ids else ''
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="uid">{escape(identifier)}</dc:identifier><dc:title>{escape(title)}</dc:title>'
           f'<dc:creator>{escape(author)}</dc:creator><dc:language>{escape(language)}</dc:language>'
           f'<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>{cover_meta}</metadata>'
           f'<manifest>{manifest}</manifest><spine toc="{ids["toc.ncx"]}">{itemrefs}</spine></package>')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')

    def add(z, name, data, compress=zipfile.ZIP_DEFLATED):
        z.writestr(zipfile.ZipInfo(name, STAMP), data, compress_type=compress)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        add(z, 'mimetype', b'application/epub+zip', zipfile.ZIP_STORED)  # must come first, uncompressed
        add(z, 'META-INF/container.xml', container.encode())
        add(z, 'OEBPS/content.opf', opf.encode())
        for name, data, media, _ in files:
            # Deflate images too: JPEGs of screenshots often shrink a further 20-25%.
            add(z, f'OEBPS/{name}', data)
    return buffer.getvalue()
