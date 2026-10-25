"""Rank a document's pages for each query: BM25 + dense + number match, fused by RRF.

The cited pages are never used in the ranking; coverage.py compares them to it afterwards.
Pilot fixes: tied pages share their average rank, pages with a number score of 0 are left
out of that ranking, IDF is fitted on PDF text only, and TOC pages are excluded.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi
from scipy import sparse
from scipy.stats import rankdata

from qc.pages import searchable
from qc.text import numbers, sha, tokenize, windows


def rrf(rankings: dict[str, dict[int, float]], pages: list[int], k: int = 60, exclude_zero: tuple[str, ...] = ("num",)) -> dict[int, float]:
    """Reciprocal rank fusion with average ranks for ties.

    `rankings` maps ranker -> {page: score}. Rankers listed in `exclude_zero` rank only the
    pages they score above 0, so a page is never ranked for a number it does not contain.
    """
    fused = {page: 0.0 for page in pages}
    for name, scores in rankings.items():
        included = [p for p in pages if not (name in exclude_zero and scores.get(p, 0.0) <= 0)]
        if not included:
            continue
        ranks = rankdata([-scores.get(p, 0.0) for p in included], method="average")
        for page, rank in zip(included, ranks):
            fused[page] += 1.0 / (k + rank)
    return fused


def order(fused: dict[int, float], rankings: dict[str, dict[int, float]]) -> list[int]:
    """Pages by fused score; exact ties fall back to BM25, then page number."""
    bm25 = rankings.get("bm25", {})
    return sorted(fused, key=lambda p: (-fused[p], -bm25.get(p, 0.0), p))


class TfidfDense:
    """Local stand-in for embeddings when no API is used: TF-IDF fitted on PDF chunks only."""

    name = "tfidf"

    def __init__(self, chunk_texts: list[str]):
        docs = [tokenize(text) for text in chunk_texts]
        vocab: dict[str, int] = {}
        for doc in docs:
            for token in doc:
                vocab.setdefault(token, len(vocab))
        self.vocab = vocab
        df = np.zeros(len(vocab))
        for doc in docs:
            for token in set(doc):
                df[vocab[token]] += 1
        self.idf = np.log((1 + len(docs)) / (1 + df)) + 1
        self.matrix = self._vectors(docs)

    def _vectors(self, docs: list[list[str]]) -> sparse.csr_matrix:
        rows, cols, vals = [], [], []
        for row, doc in enumerate(docs):
            counts: dict[int, int] = {}
            for token in doc:
                if token in self.vocab:
                    counts[self.vocab[token]] = counts.get(self.vocab[token], 0) + 1
            for col, count in counts.items():
                rows.append(row)
                cols.append(col)
                vals.append(count * self.idf[col])
        matrix = sparse.csr_matrix((vals, (rows, cols)), shape=(len(docs), len(self.vocab)))
        norms = np.sqrt(matrix.multiply(matrix).sum(axis=1)).A1
        norms[norms == 0] = 1
        return sparse.diags(1 / norms) @ matrix

    def similarities(self, queries: list[str]) -> np.ndarray:
        return (self._vectors([tokenize(q) for q in queries]) @ self.matrix.T).toarray()


class OpenAIDense:
    """OpenAI embeddings for chunks and queries, cached on disk by text hash."""

    name = "openai"

    def __init__(self, chunk_texts: list[str], backend, cache_dir: Path, doc_id: str):
        self.backend = backend
        self.doc_id = doc_id
        model = backend.config["models"]["embedding"]
        self.cache_path = cache_dir / f"embeddings.{sha(model, 8)}.json"
        self.cache: dict[str, list[float]] = {}
        if self.cache_path.is_file():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.matrix = self._embed(chunk_texts, "embed_chunks")

    def _embed(self, texts: list[str], stage: str) -> np.ndarray:
        keys = [sha(text) for text in texts]
        missing = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in self.cache))
        if missing:
            for text, vector in zip(missing, self.backend.embed(missing, self.doc_id, stage)):
                self.cache[sha(text)] = vector
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        matrix = np.array([self.cache[k] for k in keys], dtype=float)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return matrix / norms

    def similarities(self, queries: list[str]) -> np.ndarray:
        return self._embed(queries, "embed_queries") @ self.matrix.T


class PageIndex:
    def __init__(self, pages: list[dict], config: dict, dense: str = "tfidf", backend=None,
                 cache_dir: Path | None = None, doc_id: str = ""):
        self.config = config["retrieval"]
        self.pages = searchable(pages)
        self.page_ids = [page["page"] for page in self.pages]
        self.bm25 = BM25Okapi([tokenize(page["clean"]) or ["_"] for page in self.pages])
        self.page_numbers = {page["page"]: numbers(page["clean"]) for page in self.pages}
        self.chunk_pages: list[int] = []
        chunk_texts: list[str] = []
        for page in self.pages:
            words = page["clean"].split()
            for start, end in windows(len(words), self.config["dense_chunk_words"], self.config["dense_chunk_stride"]):
                self.chunk_pages.append(page["page"])
                chunk_texts.append(" ".join(words[start:end]))
        self.dense = None
        if dense == "tfidf" and chunk_texts:
            self.dense = TfidfDense(chunk_texts)
        elif dense == "openai" and chunk_texts:
            self.dense = OpenAIDense(chunk_texts, backend, cache_dir or Path("."), doc_id)
        self.dense_name = self.dense.name if self.dense else "none"

    def dense_scores(self, queries: list[str]) -> list[dict[int, float]]:
        if self.dense is None:
            return [{} for _ in queries]
        sims = self.dense.similarities(queries)
        out = []
        for row in sims:
            best: dict[int, float] = {}
            for page, value in zip(self.chunk_pages, row):
                if value > best.get(page, -np.inf):
                    best[page] = float(value)
            out.append(best)
        return out

    def bm25_scores(self, query: str) -> dict[int, float]:
        tokens = tokenize(query)
        if not tokens or not self.page_ids:
            return {page: 0.0 for page in self.page_ids}
        return dict(zip(self.page_ids, map(float, self.bm25.get_scores(tokens))))

    def number_scores(self, query_text: str) -> dict[int, float]:
        wanted = numbers(query_text)
        if not wanted:
            return {}
        return {page: len(wanted & found) / len(wanted) for page, found in self.page_numbers.items()}

    def rank_many(self, queries: list[tuple[str, str]]) -> list[dict]:
        """Rank pages for each (query, number_text) pair; number_text excludes the heading."""
        dense = self.dense_scores([query for query, _ in queries])
        results = []
        for (query, number_text), dense_scores in zip(queries, dense):
            rankings = {"bm25": self.bm25_scores(query)}
            if dense_scores:
                rankings["dense"] = dense_scores
            num = self.number_scores(number_text)
            if num:
                rankings["num"] = num
            fused = rrf(rankings, self.page_ids, self.config["rrf_k"])
            ranked = order(fused, rankings)
            results.append({"ranked": ranked, "rankings": rankings, "fused": fused})
        return results


def query_for(record: dict) -> tuple[str, str]:
    """Heading + field + text; numbers come from the text alone."""
    return f"{record['section']} {record['field']} {record['text']}", record["text"]


def retrieve(records: list[dict], index: PageIndex, id_key: str) -> list[dict]:
    k = index.config["k"]
    keep = index.config["stored_ranks"]
    out = []
    for record, result in zip(records, index.rank_many([query_for(r) for r in records])):
        ranked = result["ranked"]
        position = {page: rank for rank, page in enumerate(ranked, start=1)}
        cited_ranks = [position[p] for p in record["cited_pages"] if p in position]
        stored = ranked[:keep]
        out.append(
            {
                id_key: record[id_key],
                "topk": ranked[:k],
                "ranked": stored,
                "cited_rank": min(cited_ranks) if cited_ranks else None,
                "cited_ranks": {str(p): position.get(p) for p in record["cited_pages"]},
                "n_ranked": len(ranked),
                "scores": {
                    name: {str(p): round(scores.get(p, 0.0), 4) for p in stored}
                    for name, scores in result["rankings"].items()
                },
            }
        )
    return out
