"""Serialize layout blocks as semantic HTML that Calibre packages into a reflowable EPUB."""
from __future__ import annotations

from html import escape
from statistics import median

from ..articles import CSS as ARTICLE_CSS
from .text import collapse, should_dehyphenate

PLAIN = (False, False, False, False, False)
TAGS = ((2, 'code'), (0, 'strong'), (1, 'em'), (3, 'sup'), (4, 'sub'))  # Span.style index → element
CSS = ARTICLE_CSS + '''
aside.box { margin: 1.2em 0; padding: .6em .8em; border: 1px solid #999; background: #f2f2f2; }
aside.box p:last-child { margin-bottom: 0; }
p.note { font-size: .85em; color: #444; }
'''


def trim_hyphen(parts):
    while parts and not parts[-1][1].strip():
        parts.pop()
    style, text = parts[-1]
    parts[-1] = (style, text.rstrip()[:-1])


def joined(lines, vocabulary):
    """Join wrapped lines into one run of styled text, undoing typeset hyphenation."""
    parts = []
    for line in lines:
        if parts:
            before = ''.join(text for _, text in parts).rstrip()
            if before.endswith('-') and before[-2:-1].isalpha():
                if should_dehyphenate(before, line.text.lstrip(), vocabulary):
                    trim_hyphen(parts)  # "sepa-" + "rates"; a kept compound hyphen also needs no space
            elif not before.endswith(('—', '/')):
                parts.append((PLAIN, ' '))
        parts.extend((s.style, s.text) for s in line.spans)
    return parts


def inline(parts):
    runs = []
    for style, text in parts:
        text = collapse(text)
        if runs and (runs[-1][0] == style or not text.strip()):
            runs[-1][1] += text
        else:
            runs.append([style, text])
    output, pending_space = [], False
    for index, (style, text) in enumerate(runs):
        lead, core, tail = text[:1] == ' ', text.strip(), text[-1:] == ' '
        if not core:
            pending_space = pending_space or lead
            continue
        if (lead or pending_space) and output:
            output.append(' ')
        html = escape(core, quote=False)
        for position, tag in TAGS:
            if style[position]:
                html = f'<{tag}>{html}</{tag}>'
        output.append(html)
        pending_space = tail
    return ''.join(output)


def code(block):
    lines = block.lines
    left = min(line.box.x0 for line in lines)
    char = median((line.box.x1 - line.box.x0) / max(1, len(line.text)) for line in lines) or 6.0
    output, previous = [], None
    for line in lines:
        if previous is not None and previous.page == line.page and line.box.y0 - previous.box.y1 > 0.6 * previous.box.height:
            output.append('')
        indent = int((line.box.x0 - left) / char + 0.5)
        output.append(' ' * indent + line.text.replace(' ', ' ').rstrip())
        previous = line
    return '<pre><code>' + escape('\n'.join(output), quote=False) + '</code></pre>'


def element(block, vocabulary, image_name):
    if block.kind == 'figure':
        return f'<figure><img src="{escape(image_name(block.picture))}" alt=""/></figure>'
    if block.kind == 'code':
        return code(block)
    parts = joined(block.lines, vocabulary)
    if block.kind == 'heading':
        parts = [((False,) + style[1:], text) for style, text in parts]  # headings are bold already
    content = inline(parts)
    if not content:
        return ''
    if block.kind == 'heading':
        level = block.level
        attributes = ' class="chapter"' if level == 1 else ''
        return f'<h{level}{attributes}>{content}</h{level}>'
    if block.kind == 'note':
        return f'<p class="note">{content}</p>'
    return f'<li>{content}</li>' if block.kind == 'item' else f'<p>{content}</p>'


def body(blocks, vocabulary, image_name):
    output, region, in_list = [], None, False
    for block in blocks:
        if in_list and (block.kind != 'item' or block.region != region):
            output.append('</ul>')
            in_list = False
        if block.region != region:
            if region is not None:
                output.append('</aside>')
            if block.region is not None:
                output.append('<aside class="box">')
            region = block.region
        if block.kind == 'item' and not in_list:
            output.append('<ul>')
            in_list = True
        output.append(element(block, vocabulary, image_name))
    if in_list:
        output.append('</ul>')
    if region is not None:
        output.append('</aside>')
    return '\n'.join(part for part in output if part)


def document(blocks, title, vocabulary, image_name, language='en'):
    return (f'<!DOCTYPE html>\n<html lang="{escape(language)}"><head><meta charset="utf-8"/>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f'<title>{escape(title)}</title><style>{CSS}</style></head><body>\n'
            f'{body(blocks, vocabulary, image_name)}\n</body></html>\n')
