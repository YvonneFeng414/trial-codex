"""Absence facts ("Not reported in protocol"): candidate pages and the no-call verdicts.

C(item) = pages matching the field's term list in lexicon.yaml ∪ pages with BM25 > τ for
the item ∪ the dense top pages for the item. No candidates and no unsearchable pages means
`consistent` with no model call; no candidates but unsearchable pages means
`unverifiable`. Otherwise the judge is asked whether any passage specifies the item.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

from qc import QC_ROOT
from qc.pages import searchable
from qc.text import normalize

DEFAULT_LEXICON = QC_ROOT / "lexicon.yaml"


def load_lexicon(path: Path | None = None) -> dict:
    with open(path or DEFAULT_LEXICON, encoding="utf-8") as stream:
        lexicon = yaml.safe_load(stream)
    for group in ("fields", "headings"):
        for name, terms in lexicon.get(group, {}).items():
            bad = [term for term in terms if not isinstance(term, str) or not term.strip()]
            if bad:  # e.g. an unquoted `null`, which YAML reads as None
                raise ValueError(f"lexicon {group}.{name}: non-string terms {bad}; quote them")
    return lexicon


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern[str]:
    if term.startswith("re:"):
        return re.compile(term[3:])  # case-sensitive: acronyms such as SAS, PASS
    # Leading boundary only: "stratif" must match "stratified" and "stratification".
    return re.compile(r"(?<![\w])" + re.escape(normalize(term)).replace(r"\ ", r"\s+"), re.IGNORECASE)


def terms_for(fact: dict, lexicon: dict) -> list[str]:
    fields = lexicon.get("fields", {})
    if fact["field"] in fields:
        return list(fields[fact["field"]])
    return list(lexicon.get("headings", {}).get(fact["section"], []))


def matches_any(text: str, terms: list[str]) -> list[str]:
    text = normalize(text)
    return [term for term in terms if term_pattern(term).search(text)]


def lexicon_pages(terms: list[str], pages: list[dict]) -> list[int]:
    return [page["page"] for page in searchable(pages) if terms and matches_any(page["clean"], terms)]


def candidates(fact: dict, pages: list[dict], page_index, lexicon: dict, config: dict, dense_scores: dict[int, float] | None) -> dict:
    settings = config["absence"]
    terms = terms_for(fact, lexicon)
    by_lexicon = lexicon_pages(terms, pages)
    by_bm25: list[int] = []
    if settings.get("tau") is not None:
        scores = page_index.bm25_scores(f"{fact['field']} {fact['item']}")
        by_bm25 = [page for page, score in scores.items() if score > settings["tau"]]
    by_dense: list[int] = []
    if dense_scores:
        floor = settings.get("dense_min")
        ranked = sorted(dense_scores, key=lambda p: (-dense_scores[p], p))
        by_dense = [p for p in ranked if floor is None or dense_scores[p] >= floor][: settings["dense_top"]]
    union = sorted(set(by_lexicon) | set(by_bm25) | set(by_dense))
    return {"pages": union, "lexicon": by_lexicon, "bm25": sorted(by_bm25), "dense": by_dense,
            "terms": len(terms), "tau": settings.get("tau")}


def auto_verdict(found: dict, pages: list[dict]) -> str | None:
    """The verdict an absence fact gets without a judge call, or None if it needs one."""
    if found["pages"]:
        return None
    return "unverifiable" if any(page["unsearchable"] for page in pages) else "consistent"


def calibrate_tau(present_facts: list[dict], verdicts: dict[str, dict], page_index, target: float = 0.99) -> float | None:
    """τ for P3: the BM25 score that keeps `target` recall on present, judge-confirmed facts.

    For each supported present fact, take the best BM25 score of its item query over the
    pages that the judge said support it; τ is the (1 - target) quantile of those scores.
    """
    best: list[float] = []
    for fact in present_facts:
        verdict = verdicts.get(fact["fact_id"])
        if not verdict or verdict.get("verdict") != "supported":
            continue
        support = verdict.get("supporting_pages") or ([verdict["quote_page"]] if verdict.get("quote_page") else [])
        if not support:
            continue
        scores = page_index.bm25_scores(f"{fact['field']} {fact['item']}")
        values = [scores[p] for p in support if p in scores]
        if values:
            best.append(max(values))
    if not best:
        return None
    # Strictly below the quantile score so that facts scoring exactly τ stay candidates.
    return float(np.quantile(best, 1 - target)) - 1e-9
