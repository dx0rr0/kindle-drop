"""Plain data passed between the PDF pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Span:
    text: str
    size: float
    bold: bool = False
    italic: bool = False
    mono: bool = False
    sup: bool = False
    sub: bool = False
    baseline: float = 0.0
    width: float = 0.0

    @property
    def style(self):
        return self.bold, self.italic, self.mono, self.sup, self.sub


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return self.y1 - self.y0

    def contains(self, x, y, slack=1.0):
        return self.x0 - slack <= x <= self.x1 + slack and self.y0 - slack <= y <= self.y1 + slack


@dataclass
class Line:
    page: int
    box: Rect
    spans: list[Span]
    bullet: bool = False
    region: int | None = None  # id of the shaded frame (aside) containing this line, if any
    column: int = 0  # 0 full width, 1 left, 2 right column of a two-column page
    note: bool = False  # small print at the foot of a page or column

    @property
    def text(self):
        return ''.join(s.text for s in self.spans)

    @property
    def size(self):
        """The size carrying most characters, ignoring superscripts."""
        weights = {}
        for s in self.spans:
            if s.text.strip() and not s.sup:
                weights[round(s.size, 1)] = weights.get(round(s.size, 1), 0) + len(s.text.strip())
        return max(weights, key=weights.get) if weights else round(self.spans[0].size, 1)

    def first_word_width(self):
        """Measured from the span holding the first word, so wide monospace words count."""
        for s in self.spans:
            if s.text.strip():
                word = s.text.split()[0]
                return s.width * len(word) / max(1, len(s.text))
        return 0.0

    @property
    def mono(self):
        visible = [s for s in self.spans if s.text.strip()]
        return bool(visible) and all(s.mono for s in visible)

    @property
    def bold(self):
        visible = [s for s in self.spans if s.text.strip()]
        return bool(visible) and all(s.bold for s in visible)


@dataclass
class Picture:
    page: int
    box: Rect
    xref: int  # 0 for inline images, which have no PDF object of their own

    @property
    def key(self):
        """Identity for de-duplication: the same image object drawn twice is exported once."""
        return self.xref or (self.page, self.box.x0, self.box.y0)


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)
    bullets: list[Rect] = field(default_factory=list)
    frames: list[Rect] = field(default_factory=list)


@dataclass
class Block:
    """A reflowable unit: heading, paragraph, list item, note, code listing or figure."""
    kind: str
    lines: list[Line] = field(default_factory=list)
    level: int = 0
    region: int | None = None  # frame id for boxed asides
    picture: Picture | None = None
