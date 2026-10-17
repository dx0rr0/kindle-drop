"""Formulas as MathML, which KOReader typesets: TeX from MathJax/KaTeX pages, and page MathML.

Pages using MathJax or KaTeX ship their formulas as TeX between delimiters and typeset them
with JavaScript in the browser; without that step an e-reader shows the raw TeX. TeX is
converted with latex2mathml; MathML (the page's own or converted) keeps only presentation
elements and attributes.
"""
import re

from lxml import etree

MATHML = 'http://www.w3.org/1998/Math/MathML'
TAGS = {'math', 'mrow', 'mi', 'mn', 'mo', 'ms', 'mtext', 'mspace', 'msub', 'msup', 'msubsup', 'mfrac',
        'msqrt', 'mroot', 'mstyle', 'merror', 'mpadded', 'mphantom', 'mfenced', 'menclose', 'munder',
        'mover', 'munderover', 'mtable', 'mtr', 'mtd', 'mlabeledtr', 'mmultiscripts', 'mprescripts', 'none'}
ATTRIBUTES = {'display', 'mathvariant', 'mathsize', 'stretchy', 'fence', 'separator', 'lspace', 'rspace',
              'linethickness', 'accent', 'accentunder', 'columnalign', 'rowalign', 'columnspacing', 'rowspacing',
              'displaystyle', 'scriptlevel', 'movablelimits', 'symmetric', 'largeop', 'form', 'minsize', 'maxsize',
              'open', 'close', 'separators', 'notation', 'width', 'height', 'depth', 'columnlines', 'rowlines',
              'frame', 'rowspan', 'columnspan', 'bevelled', 'numalign', 'denomalign'}
DROP = {'annotation', 'annotation-xml'}
SKIP = {'pre', 'code', 'script', 'style', 'textarea'}
FUNCTIONS = {'arccos', 'arcsin', 'arctan', 'arg', 'cos', 'cosh', 'cot', 'coth', 'csc', 'deg', 'det', 'dim', 'exp',
             'gcd', 'hom', 'inf', 'ker', 'lg', 'lim', 'ln', 'log', 'max', 'min', 'Pr', 'sec', 'sin', 'sinh', 'sup',
             'tan', 'tanh'}
MAX_TEX = 2000
DELIMITERS = [r'\$\$(?P<a>.+?)\$\$', r'\\\[(?P<b>.+?)\\\]', r'\\\((?P<c>.+?)\\\)']
DOLLAR = r'(?<![\\$\w])\$(?![\s$])(?P<d>[^$]+?)(?<![\s\\])\$(?![\w$])'
TEX_RENDERER = re.compile(r'mathjax|katex', re.I)
# MathJax `inlineMath: [['$', '$'], ...]` or KaTeX `{left: '$', right: '$', display: false}`.
DOLLAR_CONFIG = re.compile(r"""inlineMath\s*:\s*\[\s*\[\s*['"]\$['"]|left\s*:\s*['"]\$['"]\s*,\s*right\s*:\s*['"]\$['"]\s*,\s*display\s*:\s*false""")


def local(node):
    return etree.QName(node).localname.lower()


def mathml(source, display=None):
    """A sanitized copy of a <math> element, in the MathML namespace."""
    def build(node, parent):
        name = local(node)
        name = name if name in TAGS and (parent is None or name != 'math') else 'mrow'
        element = etree.Element(f'{{{MATHML}}}{name}', nsmap={None: MATHML}) if parent is None \
            else etree.SubElement(parent, f'{{{MATHML}}}{name}')
        for key, value in node.attrib.items():
            if etree.QName(key).localname.lower() in ATTRIBUTES and len(value) <= 100:
                element.set(etree.QName(key).localname.lower(), value)
        element.text = node.text
        for child in node:
            if isinstance(child.tag, str) and local(child) not in DROP:
                build(child, element).tail = child.tail
        return element

    root = build(source, None)
    if display:
        root.set('display', display)
    return root


def tex(source, display):
    """MathML for a TeX formula, or None when it cannot be converted."""
    import latex2mathml.converter  # only for pages with formulas
    try:
        converted = latex2mathml.converter.convert(source, display='block' if display else 'inline')
        formula = mathml(etree.fromstring(converted), 'block' if display else 'inline')
    except Exception:
        return None
    # TeX's thin space after \log, \sin...: "log x", but "log(x)".
    for name in formula.iter(f'{{{MATHML}}}mi'):
        following = name.getnext()
        first = next((node for node in following.iter() if len(node) == 0), following) if following is not None else None
        if name.text in FUNCTIONS and first is not None and not (local(first) == 'mo' and (first.text or '').strip() in ('(', '[')):
            space = etree.Element(f'{{{MATHML}}}mspace', width='0.167em')
            space.tail, name.tail = name.tail, None
            name.addnext(space)
    return formula


def renderer(tree):
    """None if the page does not typeset TeX; otherwise whether single $ delimits inline TeX."""
    scripts = ' '.join((node.get('src') or '') + ' ' + (node.text or '')[:20000] for node in tree.iter('script'))
    if not TEX_RENDERER.search(scripts):
        return None
    return bool(DOLLAR_CONFIG.search(scripts))


def pieces(text, pattern):
    """[text, formula, text, formula, ..., text] with formulas as MathML (or kept as text)."""
    parts, position = [], 0
    for match in pattern.finditer(text):
        source = next(value for value in match.groupdict().values() if value is not None)
        formula = tex(source.strip(), match.lastgroup in ('a', 'b')) if len(source) <= MAX_TEX else None
        if formula is None:
            continue
        parts += [text[position:match.start()], formula]
        position = match.end()
    return parts + [text[position:]]


def typeset(body, dollars=False):
    """Replace TeX between MathJax/KaTeX delimiters in the article text with MathML."""
    pattern = re.compile('|'.join(DELIMITERS + ([DOLLAR] if dollars else [])), re.S)
    for node in list(body.iter()):
        if not isinstance(node.tag, str) or node.tag.startswith('{') or node.tag in SKIP \
                or any(isinstance(a.tag, str) and (a.tag in SKIP or a.tag.startswith('{')) for a in node.iterancestors()):
            continue
        if node.text and pattern.search(node.text):
            parts = pieces(node.text, pattern)
            node.text = parts[0]
            for index in range(1, len(parts), 2):
                node.insert(index // 2, parts[index])
                parts[index].tail = parts[index + 1]
        parent = node.getparent()
        if parent is not None and node.tail and pattern.search(node.tail) and not node.tag.startswith('{'):
            parts = pieces(node.tail, pattern)
            node.tail = parts[0]
            at = parent.index(node)
            for index in range(1, len(parts), 2):
                at += 1
                parent.insert(at, parts[index])
                parts[index].tail = parts[index + 1]
