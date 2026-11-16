# Corrupted-claim experiment (coverage)

Does Source Recall@k (D1) react when SAP facts are wrong? For 5 processed documents,
every present fact with cited pages gets each corruption from `validate/plant.py` that
applies to it (number scaling, arm swap, ≥↔≤, ITT→PP, present→"Not reported in
protocol"). Originals and corrupted copies are ranked with the production retriever and
compared, paired by kind. Coverage only: no judge, no accuracy.

```sh
cd qc-pipeline
uv run experiments/corrupted_claims/run.py                 # the 5 pinned docs, openai dense
uv run experiments/corrupted_claims/run.py --draw          # a fresh seeded draw from out/_summary.tsv
uv run experiments/corrupted_claims/run.py --dense tfidf   # no API calls
```

Inputs: `out/<doi_dir>/<stem>.{pages,facts,claims}.jsonl` (read only).
Outputs: `results/results.json` (per document and pooled, by kind) and
`results/pairs.jsonl` (each original/corrupted pair with its cited ranks). Query
embeddings are cached in `cache/` (git-ignored), seeded from each document's own cache.

Results are written up in `docs/qc-pipeline.html`, section 2.
