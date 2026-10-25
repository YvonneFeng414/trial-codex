"""Coverage = Source Recall@k (D1): does retrieval reach the cited pages?

It picks k and flags coverage misses. It is not an accuracy metric: it tracks topic, not
correctness, which the perturbation check documents (Recall@5 barely moves when claims are
corrupted).
"""

from __future__ import annotations

from math import comb


def baseline(n_pages: int, n_cited: int, k: int) -> float:
    """P(a random pick of k pages hits one of |S| cited pages) = 1 − C(n−|S|,k)/C(n,k)."""
    if n_pages <= 0 or n_cited <= 0:
        return 0.0
    k = min(k, n_pages)
    return 1 - comb(n_pages - n_cited, k) / comb(n_pages, k)


def _hit(record: dict, k: int) -> bool:
    return record["cited_rank"] is not None and record["cited_rank"] <= k


def size_bin(n: int) -> str:
    return "1" if n == 1 else "2" if n == 2 else "3-4" if n <= 4 else "5+"


def report(records: list[dict], cited: dict[str, list[int]], searchable_pages: set[int], ks: list[int], k: int) -> dict:
    """Recall@k with baseline, split by |S|, and MRR for |S| = 1.

    `cited` maps record id -> cited pages; records without specific pages are excluded.
    |S| counts only cited pages retrieval can rank, so the baseline is the fair one.
    """
    rows = []
    for record in records:
        pages = cited[record["id"]]
        if not pages:
            continue
        rows.append((record, len(set(pages) & searchable_pages), record["n_ranked"]))
    out: dict = {"n": len(rows), "k": k, "recall": {}, "baseline": {}, "by_size": {}, "unrankable": 0}
    if not rows:
        return out
    out["unrankable"] = sum(1 for _, size, _ in rows if size == 0)
    for kk in ks:
        out["recall"][kk] = sum(_hit(r, kk) for r, _, _ in rows) / len(rows)
        out["baseline"][kk] = sum(baseline(n, s, kk) for _, s, n in rows) / len(rows)
    for name in ("1", "2", "3-4", "5+"):
        group = [(r, s, n) for r, s, n in rows if size_bin(max(s, 1)) == name]
        if group:
            out["by_size"][name] = {
                "n": len(group),
                "recall": sum(_hit(r, k) for r, _, _ in group) / len(group),
                "baseline": sum(baseline(n, s, k) for _, s, n in group) / len(group),
            }
    singles = [r for r, s, _ in rows if s == 1]
    if singles:
        out["mrr_single"] = sum(1 / r["cited_rank"] if r["cited_rank"] else 0.0 for r in singles) / len(singles)
    return out


def choose_k(records: list[dict], confirmed: set[str], target: float = 0.95, k_max: int = 50) -> int | None:
    """P3: the smallest k with Recall@k >= target on facts whose page the judge confirmed."""
    rows = [r for r in records if r["id"] in confirmed]
    if not rows:
        return None
    for k in range(1, k_max + 1):
        if sum(_hit(r, k) for r in rows) / len(rows) >= target:
            return k
    return None
