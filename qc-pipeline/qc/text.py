"""Text primitives shared by every stage: normalization, tokens, numbers, passages, quotes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Iterable

_CHAR_MAP = str.maketrans(
    {
        "−": "-",  # minus sign
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        " ": " ",
        "­": "",  # soft hyphen
        "…": "...",
    }
)

STOPWORDS = frozenset(
    """a about above after again all also an and any are as at be been before being below
    between both but by can could did do does each for from further had has have having he
    her here hers him his how i if in into is it its itself just may me might more most must
    my no nor not of off on once only or other our out over own per same shall she should so
    some such than that the their them then there these they this those through to too under
    until up upon very was we were what when where which while who whom why will with within
    would you your""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def normalize(text: str) -> str:
    """NFKC (ligatures, full-width forms) plus one spelling for dashes, quotes and spaces."""
    return unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens for BM25 and TF-IDF; decimals such as 97.5 stay one token."""
    return [
        token
        for token in _TOKEN_RE.findall(normalize(text).lower())
        if token not in STOPWORDS and (len(token) > 1 or token.isdigit())
    ]


def sha(value: str | bytes | object, length: int = 16) -> str:
    if not isinstance(value, (str, bytes)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:length]


# --- numbers -----------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\d.,])"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?![\d])"
    r"(?P<pct>\s*(?:%|percent\b|per\s+cent\b))?",
    re.IGNORECASE,
)


def number_values(text: str) -> list[float]:
    """Every number in `text`, normalized: `1,000` -> 1000, `x%` -> x/100, sign dropped."""
    values: list[float] = []
    for match in _NUMBER_RE.finditer(normalize(text)):
        value = float(match.group("num").replace(",", ""))
        if match.group("pct"):
            value /= 100
        values.append(round(value, 9))
    return values


def numbers(text: str) -> set[float]:
    """Non-trivial numbers: whole numbers <= 10 carry no signal and are dropped."""
    return {value for value in number_values(text) if not (value.is_integer() and value <= 10)}


# --- passages ----------------------------------------------------------------------------


def windows(n_words: int, size: int, stride: int) -> list[tuple[int, int]]:
    """Word windows [start, end) of `size` every `stride` words, the tail always covered."""
    if n_words <= size:
        return [(0, n_words)] if n_words else []
    spans = [(start, start + size) for start in range(0, n_words - size + 1, stride)]
    if spans[-1][1] < n_words:
        spans.append((n_words - size, n_words))
    return spans


# --- quotes ------------------------------------------------------------------------------

_LINE_HYPHEN_RE = re.compile(r"(\w)-[ \t]*\n[ \t]*(?=[a-z])")
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|\[\.\.\.\])\s*")


def squash(text: str) -> str:
    """Casefolded text with every whitespace removed.

    Removing all whitespace, not just collapsing it, absorbs the letter-spacing pypdf
    produces on some layouts ("statis t i c i a n s") as well as line breaks.
    """
    return re.sub(r"\s+", "", normalize(text).casefold())


def page_variants(*texts: str) -> list[str]:
    """Squashed forms of a page to match a quote against, with and without line-end hyphens."""
    variants: list[str] = []
    for text in texts:
        if not text:
            continue
        normal = normalize(text)
        for form in (normal, _LINE_HYPHEN_RE.sub(r"\1", normal)):
            squashed = squash(form)
            if squashed not in variants:
                variants.append(squashed)
    return variants


def quote_segments(quote: str) -> list[str]:
    return [squash(part) for part in _ELLIPSIS_RE.split(normalize(quote)) if squash(part)]


def quote_matches(quote: str, variants: Iterable[str], min_chars: int = 10) -> bool:
    """True if every ellipsis-separated segment of `quote` occurs, in order, in one variant."""
    segments = quote_segments(quote or "")
    if not segments or sum(len(segment) for segment in segments) < min_chars:
        return False
    for variant in variants:
        position = 0
        for segment in segments:
            found = variant.find(segment, position)
            if found < 0:
                break
            position = found + len(segment)
        else:
            return True
    return False
