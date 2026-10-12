"""Rebuild reading order blocks from line geometry: margins, indents, gaps and fonts."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re
from statistics import median

from .model import Block, Line, Page
from .text import collapse

BULLET = re.compile(r'^\s*[•●▪■◦‣∙·]\s*')
ENUMERATOR = re.compile(r'^\s*(\(?\d{1,3}[.)]|\(?[a-z][.)]|\([ivx]+\)|\[\d{1,4}\])\s')
HEADING_RATIO = 1.18
FURNITURE_BAND = 0.09
NEIGHBOURS = 4  # lines on each side that define the local text block width
NOTE_RATIO = 0.9


@dataclass
class Metrics:
    body: float
    pitch: float  # baseline distance of body text, as a multiple of the font size
    left: dict[tuple[int, int], float] = field(default_factory=dict)  # by (page parity, column)
    right: dict[tuple[int, int], float] = field(default_factory=dict)
    local_right: dict[int, float] = field(default_factory=dict)  # by id(line)

    def indent(self, line):
        return line.box.x0 - self.left[(line.page % 2, line.column)]

    def right_edge(self, line):
        """Right edge of the surrounding text block: abstracts, quotes and frames are narrower.

        Lines under 60% of the column are measured against the column itself, so runs of
        short entries (indexes, addresses, verse) are not mistaken for a narrow block.
        """
        key = (line.page % 2, line.column)
        column_right = self.right[key]
        if line.box.x1 - self.left[key] < 0.6 * (column_right - self.left[key]):
            return max(column_right, self.local_right[id(line)])
        return self.local_right[id(line)]


def frame_ids(pages):
    """Number shaded frames globally; a frame resuming at the top of the next page keeps its id."""
    ids, next_id, carry = {}, 0, None
    for page in pages:
        top = min((line.box.y0 for line in page.lines), default=0)
        for index, frame in enumerate(page.frames):
            if carry is not None and frame.y0 <= top + 1:
                ids[(page.number, index)] = carry
            else:
                ids[(page.number, index)], next_id = next_id, next_id + 1
        last = page.lines[-1] if page.lines else None
        carry = None
        if last is not None:
            index = next((i for i, f in enumerate(page.frames) if f.contains(*center(last))), None)
            carry = ids[(page.number, index)] if index is not None else None
    return ids


def center(line):
    return (line.box.x0 + line.box.x1) / 2, (line.box.y0 + line.box.y1) / 2


def assign_columns(page, body):
    """Detect two-column pages: many body lines confined to either half of the page."""
    middle, slack = page.width / 2, 0.02 * page.width
    text = [l for l in page.lines if abs(l.size - body) <= 0.8 and not l.mono]
    left = [l for l in text if l.box.x1 <= middle + slack]
    right = [l for l in text if l.box.x0 >= middle - slack]
    if len(left) < 5 or len(right) < 5 or len(left) + len(right) < 0.7 * len(text):
        return
    for line in page.lines:
        line.column = 2 if line.box.x0 >= middle - slack else 1 if line.box.x1 <= middle + slack else 0


def mark_notes(page, body):
    """Footnotes: a run of small, non-code lines in the lower half of a column, below body text."""
    for column in {line.column for line in page.lines}:
        lines = sorted((l for l in page.lines if l.column == column), key=lambda l: -l.box.y0)
        run = []
        for line in lines:
            if (line.size >= NOTE_RATIO * body or line.mono or line.region is not None
                    or line.box.y0 < page.height / 2):
                break
            run.append(line)
        if len(run) < len(lines):  # an all-small column is just small print, not notes
            for line in run:
                line.note = True


def annotate(pages):
    """Attach columns, drawn bullets, bullet glyphs and shaded frames to their lines."""
    ids = frame_ids(pages)
    body = body_size(pages)
    for page in pages:
        for line in page.lines:
            index = next((i for i, f in enumerate(page.frames) if f.contains(*center(line))), None)
            line.region = ids[(page.number, index)] if index is not None else None
        assign_columns(page, body)
        mark_notes(page, body)
        for line in page.lines:
            line.bullet = any(b.y0 >= line.box.y0 - 2 and b.y1 <= line.box.y1 + 2
                              and line.box.x0 - 30 <= b.x1 <= line.box.x0 + 1 for b in page.bullets)
            first = next(s for s in line.spans if s.text.strip())
            if BULLET.match(first.text):
                first.text = BULLET.sub('', first.text)
                line.bullet = True


def body_size(pages):
    sizes = Counter()
    for page in pages:
        for line in page.lines:
            for s in line.spans:
                if s.text.strip() and not s.sup and not s.mono:
                    sizes[round(s.size * 2) / 2] += len(s.text)
    return sizes.most_common(1)[0][0] if sizes else 11.0


def isolated_margin_lines(page, body):
    """Small-print lines inside a margin band, set apart from the text block by a clear gap."""
    found = []
    for top in (True, False):
        band = page.height * FURNITURE_BAND
        inside = [l for l in page.lines if (l.box.y1 < band if top else l.box.y0 > page.height - band)]
        rest = [l for l in page.lines if not any(l is i for i in inside)]
        if not inside or len(inside) > 3 or not rest or max(l.size for l in inside) > 1.15 * body:
            continue
        height = max(l.box.height for l in inside)
        gap = (min(l.box.y0 for l in rest) - max(l.box.y1 for l in inside) if top
               else min(l.box.y0 for l in inside) - max(l.box.y1 for l in rest))
        if gap > 1.2 * height:
            found.extend(inside)
    return found


def remove_furniture(pages):
    """Drop running headers, footers and page numbers that sit in the page margins."""
    body = body_size(pages)
    seen, candidates, drop = Counter(), [], set()
    for page in pages:
        drop.update(id(line) for line in isolated_margin_lines(page, body))
        for line in page.lines:
            if line.box.y1 < page.height * FURNITURE_BAND or line.box.y0 > page.height * (1 - FURNITURE_BAND):
                key = re.sub(r'\d+', '#', collapse(line.text).strip().lower())
                key = re.sub(r'^[ivxlc]+$', '#', key)
                seen[key] += 1
                candidates.append((line, key))
    drop.update(id(line) for line, key in candidates if key == '#' or seen[key] >= 3)
    for page in pages:
        page.lines = [line for line in page.lines if id(line) not in drop]


def margin(xs):
    """Leftmost x shared by at least 15% of lines: robust to indents, lists and code."""
    counts = Counter(round(x) for x in xs)
    for x in sorted(counts):
        if sum(counts[x + d] for d in range(-2, 3)) >= 0.15 * len(xs):
            return x
    return min(counts)


def measure(pages):
    body = body_size(pages)
    lefts, rights, pitches = {}, {}, []
    for page in pages:
        previous = None
        for line in page.lines:
            if abs(line.size - body) > 0.8 or line.mono or line.region is not None:
                previous = None
                continue
            for key in ((page.number % 2, line.column), (None, line.column)):
                lefts.setdefault(key, []).append(line.box.x0)
                rights.setdefault(key, []).append(line.box.x1)
            if previous is not None and 0.8 * body < line.box.y0 - previous.box.y0 < 2.2 * body:
                pitches.append((line.box.y0 - previous.box.y0) / body)
            previous = line
    metrics = Metrics(body=body, pitch=median(pitches) if pitches else 1.2)
    for parity in (0, 1):
        for column in (0, 1, 2):
            # Mirrored margins need enough lines per parity; otherwise use both parities, then full width.
            candidates = ((parity, column), (None, column), (None, 0))
            key = next((k for k in candidates if len(lefts.get(k, ())) >= (40 if k[0] is not None else 1)), None)
            right = sorted(rights[key]) if key else [540.0]
            metrics.left[(parity, column)] = margin(lefts[key]) if key else 72.0
            metrics.right[(parity, column)] = right[int(0.9 * (len(right) - 1))]
    for page in pages:
        streams = {}
        for line in page.lines:
            streams.setdefault((line.column, line.region, line.note), []).append(line)
        for lines in streams.values():
            for index, line in enumerate(lines):
                near = lines[max(0, index - NEIGHBOURS):index + NEIGHBOURS + 1]
                metrics.local_right[id(line)] = max(n.box.x1 for n in near)
    return metrics


def kind(line, metrics):
    if line.note:
        return 'note'
    if line.mono:
        return 'code'
    text = collapse(line.text).strip()
    if (line.region is None and line.size >= metrics.body * HEADING_RATIO
            and len(text) < 160 and any(c.isalpha() for c in text)):
        return 'heading'
    return 'text'


def ended(previous, line, metrics):
    """A line that stopped early although the next word would have fitted ends its paragraph."""
    if not line.text.strip():
        return False
    space = 0.3 * previous.size
    room = metrics.right_edge(previous) - previous.box.x1
    return room > line.first_word_width() + 2 * space


def aligned(block, line, metrics, following=None):
    tolerance = 0.4 * line.size
    x, first = metrics.indent(line), metrics.indent(block.lines[0])
    if len(block.lines) > 1:
        return abs(x - metrics.indent(block.lines[1])) <= tolerance
    if abs(x - first) <= tolerance:
        return True
    if x < first:  # first-line indent
        return first - x <= 4 * line.size
    if x - first > 3 * line.size:
        return False
    # Hanging indent: bullets, "2." or "[12]" markers, or bibliography-style entries.
    if block.kind == 'item' or ENUMERATOR.match(block.lines[0].text):
        return True
    if following is None or following.page != line.page:
        return False
    # Hanging text stays indented (or ends here); a first-line indent returns to the margin.
    return abs(metrics.indent(following) - x) <= tolerance or ended(line, following, metrics)


def joins(block, line, line_kind, metrics, following=None):
    previous = block.lines[-1]
    base = 'text' if block.kind in ('item', 'note') else block.kind
    if line_kind != base or line.region != block.region or line.bullet:
        return False
    same_page = previous.page == line.page
    size = line.size
    if line_kind == 'heading':
        return same_page and abs(size - previous.size) < 0.6 and line.box.y0 - previous.box.y1 < 0.8 * size
    if line_kind == 'code':
        return not same_page or line.box.y0 - previous.box.y1 < 2.5 * size
    if same_page and line.box.y0 - previous.box.y0 > metrics.pitch * size * 1.45:
        return False
    if ended(previous, line, metrics):
        return False
    return aligned(block, line, metrics, following)


def ordered(page):
    """Interleave pictures with lines by vertical position, keeping the text stream order."""
    items, pictures = [], sorted(page.pictures, key=lambda p: p.box.y0)
    for line in page.lines:
        while pictures and (pictures[0].box.y0 + pictures[0].box.y1) / 2 <= line.box.y0:
            items.append(pictures.pop(0))
        items.append(line)
    return items + pictures


def build_blocks(pages, metrics):
    blocks, current, deferred, note = [], None, [], None
    stream = [line for page in pages for line in page.lines]
    following = {id(a): b for a, b in zip(stream, stream[1:])}
    for page in pages:
        for item in ordered(page):
            # Figures and footnotes interrupt paragraphs that continue after them; emit them after.
            if not isinstance(item, Line):
                deferred.append(Block('figure', picture=item))
                continue
            line_kind = kind(item, metrics)
            if line_kind == 'note':
                if note is not None and joins(note, item, 'text', metrics):
                    note.lines.append(item)
                else:
                    note = Block('note', [item])
                    deferred.append(note)
                continue
            note = None
            if current is not None and joins(current, item, line_kind, metrics, following.get(id(item))):
                current.lines.append(item)
                continue
            blocks.extend(deferred)
            deferred = []
            block_kind = 'item' if line_kind == 'text' and item.bullet else line_kind
            current = Block(block_kind, [item], region=item.region)
            blocks.append(current)
    blocks.extend(deferred)
    assign_levels(blocks, metrics)
    return blocks


def assign_levels(blocks, metrics):
    """Rank heading sizes; sizes within 7% are one level (pages are often scaled slightly)."""
    headings = [b for b in blocks if b.kind == 'heading']
    levels = []  # largest size of each level, descending
    for size in sorted({b.lines[0].size for b in headings}, reverse=True):
        if not levels or size < levels[-1] * 0.93:
            levels.append(size)
    for block in headings:
        size = block.lines[0].size
        block.level = min(next(i for i, top in enumerate(levels) if size >= top * 0.93) + 1, 4)


def reflow(pages):
    remove_furniture(pages)
    annotate(pages)
    metrics = measure(pages)
    return build_blocks(pages, metrics)
