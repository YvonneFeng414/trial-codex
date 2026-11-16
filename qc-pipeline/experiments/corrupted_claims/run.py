#!/usr/bin/env python3
"""Corrupted-claim experiment for coverage (D1): does Source Recall@k notice wrong facts?

For a seeded sample of processed documents, every present fact with cited pages gets each
corruption from validate/plant.py that applies to it (number scaling, arm swap, ≥↔≤ flip,
ITT→PP, present→"Not reported in protocol"). Originals and corrupted copies are ranked with
the production retriever (BM25 + dense + number match, RRF), and Recall@k is compared on
the same facts, paired by corruption kind. No judge runs: coverage only, no accuracy.

  uv run experiments/corrupted_claims/run.py                 # the 5 pinned docs, openai dense
  uv run experiments/corrupted_claims/run.py --draw          # a fresh seeded draw of 5
  uv run experiments/corrupted_claims/run.py --dense tfidf   # no API calls

Reads out/<doi_dir>/<stem>.{pages,facts,claims}.jsonl; never writes under out/. Query
embeddings are cached in experiments/corrupted_claims/cache/ (seeded from the document's
own cache, so page chunks are not re-embedded).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from qc import QC_ROOT, coverage
from qc.config import load as load_config
from qc.retrieve import PageIndex, retrieve
from qc.triggers import arm_labels
from validate.plant import KINDS, corrupt

LOST_K = 5  # hit→miss / miss→hit transitions are counted at the production k
# triggers.arm_labels only finds labelled arms ("TAH: ...") under Intervention Summary, which
# few SAPs have. Without them, arm_swap swaps these words when the facts use both.
GENERIC_ARMS = ("intervention", "control")
# The 5 documents of the reported run: pick_docs(seed 2026) over out/_summary.tsv on
# 2026-10-25, pinned because a running batch keeps adding finished documents to it.
DOCS = (
    "10.1001_jama.2015.5213/joi150052supp1_prod_protocol",
    "10.1001_jama.2019.18714/joi190134supp1_prod_protocol",
    "10.1001_jama.2022.9265/joi220058supp1_prod_1655397799.0608_protocol",
    "10.1001_jama.2024.26429/joi240152supp1_prod_1742927571.31668_protocol",
    "10.1136_bmj-2023-078876/quim078876.ww_protocol",
)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def pick_docs(out: Path, n: int, seed: int) -> list[str]:
    """doc_ids of n documents drawn from the finished ones in out/_summary.tsv."""
    with open(out / "_summary.tsv", newline="", encoding="utf-8") as stream:
        done = sorted(row["doc_id"] for row in csv.DictReader(stream, delimiter="\t") if row["status"] == "ok")
    done = [d for d in done if (out / f"{d}.facts.jsonl").is_file()]
    if len(done) < n:
        raise SystemExit(f"error: only {len(done)} finished documents in {out}")
    return sorted(random.Random(seed).sample(done, n))


def hits(rows: list[dict], k: int) -> list[bool]:
    return [r["cited_rank"] is not None and r["cited_rank"] <= k for r in rows]


def compare(before: list[dict], after: list[dict], ks: list[int], baselines: list[float]) -> dict:
    """Paired Recall@k for the same facts, original vs corrupted."""
    n = len(before)
    out = {"n": n, "original": {}, "corrupted": {}, "baseline@5": sum(baselines) / n}
    for k in ks:
        out["original"][k] = sum(hits(before, k)) / n
        out["corrupted"][k] = sum(hits(after, k)) / n
    b, a = hits(before, LOST_K), hits(after, LOST_K)
    out[f"lost@{LOST_K}"] = sum(x and not y for x, y in zip(b, a))
    out[f"gained@{LOST_K}"] = sum(y and not x for x, y in zip(b, a))
    deltas = [(y["cited_rank"] or 0) - (x["cited_rank"] or 0) for x, y in zip(before, after)
              if x["cited_rank"] and y["cited_rank"]]
    out["mean_rank_shift"] = sum(deltas) / len(deltas) if deltas else None
    out["rank_unchanged"] = sum(d == 0 for d in deltas) / len(deltas) if deltas else None
    return out


def pool(parts: list[dict], ks: list[int]) -> dict:
    """Fact-weighted pooling of compare() results."""
    n = sum(p["n"] for p in parts)
    if not n:
        return {"n": 0}
    weighted = lambda key, k=None: sum(p["n"] * (p[key][k] if k is not None else p[key]) for p in parts) / n  # noqa: E731
    shifts = [(p["n"], p["mean_rank_shift"]) for p in parts if p["mean_rank_shift"] is not None]
    same = [(p["n"], p["rank_unchanged"]) for p in parts if p["rank_unchanged"] is not None]
    return {
        "n": n,
        "original": {k: weighted("original", k) for k in ks},
        "corrupted": {k: weighted("corrupted", k) for k in ks},
        "baseline@5": weighted("baseline@5"),
        f"lost@{LOST_K}": sum(p[f"lost@{LOST_K}"] for p in parts),
        f"gained@{LOST_K}": sum(p[f"gained@{LOST_K}"] for p in parts),
        "mean_rank_shift": sum(w * s for w, s in shifts) / sum(w for w, _ in shifts) if shifts else None,
        "rank_unchanged": sum(w * s for w, s in same) / sum(w for w, _ in same) if same else None,
    }


def run_doc(doc_id: str, args, config: dict, cost) -> tuple[dict, list[dict]]:
    doi_dir, stem = doc_id.split("/", 1)
    work = args.out / doi_dir
    pages = read_jsonl(work / f"{stem}.pages.jsonl")
    facts = read_jsonl(work / f"{stem}.facts.jsonl")
    arms = arm_labels(read_jsonl(work / f"{stem}.claims.jsonl"))
    arm_source = "labelled"
    if len(arms) < 2:
        text = " ".join(f["text"] for f in facts)
        arms, arm_source = [], "none"
        if all(re.search(rf"\b{w}\b", text) for w in GENERIC_ARMS):
            arms, arm_source = list(GENERIC_ARMS), "generic"

    backend = cache = None
    if args.dense == "openai":
        from qc.llm import OpenAIBackend, require_api_key

        require_api_key()
        backend = OpenAIBackend(config, cost)
        cache = args.cache / doi_dir
        if not cache.is_dir():
            cache.mkdir(parents=True)
            for path in (work / "cache").glob("embeddings.*.json"):
                shutil.copy(path, cache / path.name)
    index = PageIndex(pages, config, dense=args.dense, backend=backend, cache_dir=cache, doc_id=doc_id)
    searchable = set(index.page_ids)

    eligible = [f for f in facts if f["status"] == "present" and f["cited_pages"]]
    original = {r["fact_id"]: r for r in retrieve(eligible, index, "fact_id")}
    rng = random.Random(f"{args.seed}:{doc_id}")
    result: dict = {"doc_id": doc_id, "n_pages": len(pages), "searchable_pages": len(searchable),
                    "eligible_facts": len(eligible), "arms": arms, "arm_source": arm_source, "by_kind": {}}
    examples = []
    for kind in KINDS:
        pairs = [(f, bad) for f in eligible if (bad := corrupt(f, kind, rng, arms)) is not None]
        if not pairs:
            result["by_kind"][kind] = {"n": 0}
            continue
        after = retrieve([bad for _, bad in pairs], index, "fact_id")
        before = [original[f["fact_id"]] for f, _ in pairs]
        baselines = [coverage.baseline(r["n_ranked"], len(set(f["cited_pages"]) & searchable), 5)
                     for f, r in zip((f for f, _ in pairs), before)]
        result["by_kind"][kind] = compare(before, after, args.ks, baselines)
        for (fact, bad), b, a in zip(pairs, before, after):
            examples.append({"doc_id": doc_id, "fact_id": fact["fact_id"], "kind": kind,
                             "section": fact["section"], "field": fact["field"],
                             "original": fact["text"], "corrupted": bad["text"],
                             "cited_pages": fact["cited_pages"],
                             "original_rank": b["cited_rank"], "corrupted_rank": a["cited_rank"]})
    result["dense"] = index.dense_name
    return result, examples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=QC_ROOT / "out", help="processed QC outputs")
    parser.add_argument("--docs", nargs="*", help="doc_ids (doi_dir/stem); default: the pinned DOCS")
    parser.add_argument("--draw", action="store_true", help="draw --n-docs finished documents with --seed instead")
    parser.add_argument("--n-docs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--dense", choices=("openai", "tfidf", "none"), default="openai")
    parser.add_argument("--results", type=Path, default=HERE / "results")
    parser.add_argument("--cache", type=Path, default=HERE / "cache")
    args = parser.parse_args()

    config = load_config()
    args.ks = config["retrieval"]["k_report"]
    docs = args.docs or (pick_docs(args.out, args.n_docs, args.seed) if args.draw else list(DOCS))
    args.results.mkdir(parents=True, exist_ok=True)

    from qc.llm import CostLog

    cost = CostLog(args.results / "cost.tsv")
    per_doc, examples = [], []
    for doc_id in docs:
        result, rows = run_doc(doc_id, args, config, cost)
        per_doc.append(result)
        examples += rows
        five = {kind: v.get("corrupted", {}).get(5) for kind, v in result["by_kind"].items()}
        print(f"{doc_id}: {result['eligible_facts']} facts; Recall@5 corrupted by kind {five}")

    pooled = {kind: pool([d["by_kind"][kind] for d in per_doc if d["by_kind"][kind]["n"]], args.ks) for kind in KINDS}
    # "Any corruption": every (fact, kind) pair; a fact counts once per kind that applies to it.
    pooled["all"] = pool([d["by_kind"][k] for d in per_doc for k in KINDS if d["by_kind"][k]["n"]], args.ks)
    # The token-level kinds alone, without present→absent, which replaces the whole text.
    pooled["edits"] = pool([d["by_kind"][k] for d in per_doc for k in KINDS
                            if k != "to_absent" and d["by_kind"][k]["n"]], args.ks)
    summary = {"seed": args.seed, "dense": per_doc[0]["dense"] if per_doc else args.dense,
               "k": config["retrieval"]["k"], "ks": args.ks, "docs": docs, "pooled": pooled, "per_doc": per_doc}
    (args.results / "results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(args.results / "pairs.jsonl", "w", encoding="utf-8") as stream:
        for row in examples:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\npooled over {len(docs)} documents (dense = {summary['dense']}):")
    print(f"{'kind':16}{'n':>6}" + "".join(f"{f'R@{k} orig→corr':>18}" for k in args.ks) + f"{'lost@5':>8}{'gained@5':>10}")
    for kind, v in pooled.items():
        if not v["n"]:
            print(f"{kind:16}{0:>6}")
            continue
        cells = "".join(f"{v['original'][k]:>9.3f}→{v['corrupted'][k]:<8.3f}" for k in args.ks)
        print(f"{kind:16}{v['n']:>6}{cells}{v['lost@5']:>8}{v['gained@5']:>10}")
    print(f"\n-> {args.results}/results.json, pairs.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
