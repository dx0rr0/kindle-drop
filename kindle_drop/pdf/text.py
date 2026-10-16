"""Repair text-layer defects using the document's own vocabulary as evidence."""
from __future__ import annotations

from collections import Counter
import re
import unicodedata

from .. import language

LIGATURES = ('fi', 'fl', 'ff', 'ffi', 'ffl', 'ft', 'st')
WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*")
TOKEN = re.compile(r'\S+')


def suspicious(char):
    """Glyphs a broken ToUnicode map tends to emit in place of ligatures."""
    if char == '�':
        return True
    return unicodedata.category(char) in ('Lm', 'Sk', 'Co', 'Cn', 'So')


class Vocabulary:
    def __init__(self, lines):
        self.words = Counter()
        for line in lines:
            for token in TOKEN.findall(line.rstrip('-')):
                if token.isascii() or not any(suspicious(c) for c in token):
                    for match in WORD.finditer(token):
                        self.words[match.group().lower()] += 1

    def __contains__(self, word):
        return word.lower() in self.words


def ligature_map(lines, vocabulary):
    """Rank, per broken glyph, the ligatures that turn affected words into known words.

    One glyph can stand for several ligatures (fi and ff share it in some fonts), so
    repair() still checks each word and only falls back to the best-ranked candidate.
    """
    votes = {}
    for line in lines:
        for token in TOKEN.findall(line):
            if token.isascii():
                continue
            for char in {c for c in token if suspicious(c)}:
                tally = votes.setdefault(char, Counter())
                for candidate in LIGATURES:
                    if letters(token).replace(char, candidate) in vocabulary:
                        tally[candidate] += 1
    return {char: [c for c, _ in tally.most_common()] for char, tally in votes.items() if tally}


def letters(token):
    return ''.join(ch for ch in token if ch.isalpha() or suspicious(ch))


def replacements(text, mapping, vocabulary):
    """Map each broken-glyph position in text to the ligature that completes its word."""
    chosen = {}
    for match in TOKEN.finditer(text):
        token = match.group()
        for offset, char in enumerate(token):
            candidates = mapping.get(char)
            if candidates:
                fitting = [c for c in candidates if letters(token).replace(char, c) in vocabulary]
                chosen[match.start() + offset] = (fitting or candidates)[0]
    return chosen


def should_dehyphenate(head, tail, vocabulary):
    """head ends with '-' at a line break; tail starts the next line."""
    left = WORD.findall(head[:-1])
    right = WORD.findall(tail)
    if not left or not right or not head[-2:-1].isalpha() or not tail[:1].islower():
        return False
    left, right = left[-1], right[0]
    joined, compound = left + right, f'{left}-{right}'
    if compound in vocabulary and joined not in vocabulary:
        return False
    if joined in vocabulary:
        return True
    # Unknown either way: typesetters hyphenate fragments, authors hyphenate whole words.
    return not (left in vocabulary and right in vocabulary and len(left) > 2)


def collapse(text):
    return re.sub(r'[\s ]+', ' ', text)


def guess_language(vocabulary):
    return language.guess(vocabulary.words)
