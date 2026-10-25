"""Evidence passages for the judge, and the grouping of facts into judge calls.

Evidence for a fact = its top passages from cited pages ∪ Top_k, plus at least one passage
from every cited page (pilot claim C90 failed without this). A coverage miss also gets its
whole cited pages. Facts that share a section are judged together on the union of their
passages, so a passage is sent once per call.
"""

from __future__ import annotations

import numpy as np
from rank_bm25 import BM25Okapi

from qc.text import tokenize, windows


class PassageIndex:
    """Fixed-size passages over every page that has text, TOC pages included (a TOC page can
    be cited). BM25 here is fitted on these PDF passages alone."""

    def __init__(self, pages: list[dict], config: dict):
        size = config["packets"]["passage_words"]
        stride = config["packets"]["passage_stride"]
        self.passages: list[dict] = []
        self.by_page: dict[int, list[int]] = {}
        for page in pages:
            if page["unsearchable"]:
                continue
            words = page["clean"].split()
            for number, (start, end) in enumerate(windows(len(words), size, stride)):
                self.by_page.setdefault(page["page"], []).append(len(self.passages))
                self.passages.append(
                    {"id": f"p{page['page']}.{number}", "page": page["page"], "text": " ".join(words[start:end])}
                )
        self.bm25 = BM25Okapi([tokenize(p["text"]) or ["_"] for p in self.passages]) if self.passages else None

    def scores(self, query: str) -> np.ndarray:
        tokens = tokenize(query)
        if self.bm25 is None or not tokens:
            return np.zeros(len(self.passages))
        return self.bm25.get_scores(tokens)

    def on_pages(self, pages) -> list[int]:
        return [index for page in sorted(set(pages)) for index in self.by_page.get(page, [])]

    def best(self, query: str, pages, n: int = 1, scores: np.ndarray | None = None) -> list[int]:
        candidates = self.on_pages(pages)
        if not candidates:
            return []
        scores = self.scores(query) if scores is None else scores
        return sorted(candidates, key=lambda i: (-scores[i], i))[:n]


def fact_query(fact: dict) -> str:
    return f"{fact['section']} {fact['field']} {fact['text']}"


def item_query(fact: dict) -> str:
    """What an absence fact says is missing, with its outcome so the right outcome ranks first."""
    return f"{fact['field']} {fact['item']} {fact.get('outcome_block') or ''}".strip()


def present_evidence(fact: dict, retrieval: dict, triggers: list[str], index: PassageIndex, config: dict) -> list[int]:
    settings = config["packets"]
    scores = index.scores(fact_query(fact))
    cited = fact["cited_pages"]
    chosen = index.best("", set(cited) | set(retrieval["topk"]), settings["top_passages"], scores)
    for page in cited:
        chosen += index.best("", [page], 1, scores)
    if "COVERAGE_MISS" in triggers:
        chosen += index.on_pages(cited[: settings["coverage_miss_whole_pages"]])
    return list(dict.fromkeys(chosen))


def absence_evidence(fact: dict, candidate_pages: list[int], index: PassageIndex, config: dict,
                     terms: list[str] | tuple[str, ...] = ()) -> list[int]:
    """One passage per candidate page, strongest first, capped at absence.max_passages.

    A passage holding one of the field's lexicon terms beats a higher BM25 score: the
    item queries are generic ("Transformations / Derived Variables"), and ranking by
    score alone dropped LACE p56, the page that contradicts pilot claim C96.
    """
    from qc.absence import matches_any

    scores = index.scores(item_query(fact))
    has_term = {i: bool(terms and matches_any(index.passages[i]["text"], list(terms)))
                for i in index.on_pages(candidate_pages)}
    best = []
    for page in candidate_pages:
        ids = index.on_pages([page])
        if ids:
            best.append(max(ids, key=lambda i: (has_term[i], scores[i], -i)))
    best.sort(key=lambda i: (not has_term[i], -scores[i], i))
    return best[: config["absence"]["max_passages"]]


def group_calls(facts: list[dict], config: dict) -> list[list[dict]]:
    """Facts of one section and kind per call, split into balanced calls of at most 8."""
    target = config["packets"]["facts_per_call"]
    cap = config["packets"]["max_facts_per_call"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for fact in facts:
        groups.setdefault((fact["section"], fact["status"]), []).append(fact)
    calls: list[list[dict]] = []
    for members in groups.values():
        n_calls = max(1, -(-len(members) // cap), round(len(members) / target))
        size = -(-len(members) // n_calls)
        calls += [members[start : start + size] for start in range(0, len(members), size)]
    return calls
