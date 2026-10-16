"""Guess a text's language: readers hyphenate by the declared one, so a frequent-word vote is worth having."""
from __future__ import annotations

from collections import Counter
import re

WORD = re.compile(r"[^\W\d_]+")

STOPWORDS = {
    'en': {'the', 'and', 'of', 'to', 'is', 'that', 'with', 'for', 'this', 'are'},
    'es': {'el', 'la', 'de', 'que', 'los', 'las', 'del', 'para', 'con', 'una'},
    'fr': {'le', 'les', 'des', 'est', 'une', 'pour', 'dans', 'qui', 'pas', 'sur'},
    'de': {'der', 'die', 'und', 'das', 'ist', 'nicht', 'mit', 'den', 'ein', 'sich'},
    'it': {'il', 'che', 'della', 'per', 'una', 'sono', 'gli', 'del', 'non', 'nel'},
    'pt': {'que', 'não', 'uma', 'para', 'com', 'os', 'das', 'dos', 'ao', 'mais'},
}


def guess(words):
    """words: a Counter of lowercase words, or text to count."""
    if isinstance(words, str):
        words = Counter(w.lower() for w in WORD.findall(words))
    scores = {lang: sum(words[w] for w in common) for lang, common in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else 'en'
