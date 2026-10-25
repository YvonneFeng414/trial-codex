# SAP QC: Implementation Plan

Checks each generated SAP (`*_protocol.md`) against its protocol PDF for **faithfulness**:
are its claims and citations right? Design and pilot: `docs/qc-pipeline.html`.

**Out of scope:** completeness, i.e. how many fields are filled. That is already measured
by the completeness metric (`docs/completeness-metric.html`, `completeness_label2.tsv`).
QC does not recompute it.

---

## 1. Fixed decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Coverage** = Source Recall@k. It only picks k and flags coverage misses; it is never reported as accuracy | It changes little when claims are corrupted: 0.97 → 0.91 with 58 corrupted claims |
| D2 | **Finding-error accuracy** = acc over T, where T is every fact a trigger flags, all judged | Shows how often flagged facts are wrong and whether the triggers find errors efficiently |
| D3 | **Random accuracy** = acc over R, a systematic 30% draw from *all* facts (π = 0.3, seed recorded) | Estimates document accuracy without bias. The errors in R∩U give the trigger miss rate |
| D4 | The unit is the **atomic fact**; both fact-level and claim-level counts are reported | One wrong criterion shouldn't hide inside a "supported" bundle |
| D5 | Page text comes from **`tools/extract_pdf.py`**, the generator's own extractor and page markers | Citation page N then matches the page index by construction (δ = 0), with no offset to estimate |
| D6 | Judge = OpenAI SDK, Pydantic structured output, model **≠ gpt-5.6-terra / gpt-5.6-luna** | Team standard. A model grading its own or its reviewer's output is not an independent check |
| D7 | The judge never sees `work/<id>.evidence.md` or `.review.md` | Keeps the check independent of the generator's reasoning |
| D8 | v1 uses the **retrieval route for every document**, even short ones | Every verdict is made from the same kind of evidence, so documents are comparable. The full-PDF route is an option for later (P5) |
| D9 | Every supported or contradicted verdict carries a **verbatim quote**, which is checked by string match against the pages shown | Catches a judge that invents its support, at no model cost |

---

## 2. Layout

```
qc/
  implementation_plan.md
  qc/                      # package
    pages.py               # extract_pdf.py → pages.jsonl; scanned-page and TOC flags
    claims.py              # SAP → claims (reuses lint_sap block/citation parsers)
    atomize.py             # claims → atomic facts (one LLM call per SAP, cached)
    retrieve.py            # BM25 + dense + number match, RRF → Top_k
    coverage.py            # Recall@k, random baseline, split by |S|
    triggers.py            # one function per trigger → reason codes
    sample.py              # systematic 30% draw
    packets.py             # evidence passages (≥1 per cited page, deduplicated)
    judge.py               # OpenAI call, quote check, fixed-check overrides
    absence.py             # absence facts: term lists + BM25 threshold + dense
    metrics.py             # acc_T, acc_R, miss rate, CIs, pooled model
    run_qc.py              # CLI: one document, or a batch over pairs.csv
  lexicon.yaml             # fixed absence term lists per SAP heading (versioned)
  validate/
    plant.py               # planted-error generator
    gold_sheet.py          # export / import of human gold labels
  tests/
  out/<doi_dir>/           # facts, retrieval, verdicts, <id>.qc.csv
  out/_summary.tsv  out/_coverage.tsv  out/_cost.tsv
```

---

## 3. Data contracts (JSONL, one record per line)

**Fact**
```json
{"doc_id": "...", "claim_id": 96, "fact_id": "96.0", "section": "5.2 Secondary Outcomes",
 "field": "Transformations / Derived Variables", "outcome_block": "Health-services use ...",
 "text": "...", "item": "derived variables", "value": null, "status": "absent",
 "cited_pages": [], "sweep": [1, 96], "md_line": 178}
```

**Retrieval:** `{fact_id, topk: [..], cited_rank: int|null, scores: {bm25, dense, num}}`

**Verdict**
```json
{"fact_id": "96.0", "in_T": true, "in_R": true, "triggers": ["ABSENCE_HITS", "SELF_CONTRA"],
 "verdict": "contradicted", "quote": "Change in quality of life: ... between baseline ... and 6 months",
 "quote_page": 56, "quote_verified": true, "override": null,
 "judge_model": "...", "prompt_sha": "...", "evidence_pages": [56, 19, 27]}
```

Allowed verdicts: `supported | contradicted | not_found` for present facts, and
`consistent | contradicted | unverifiable` for absence facts.

---

## 4. Components

### 4.1 `pages.py`
- Run `tools/extract_pdf.py <pdf> <txt>`, then split the text on `===== PAGE N OF M =====` into `pages.jsonl`.
- **Scanned pages:** pages with fewer than 20 non-whitespace characters are flagged `unsearchable`. The PDF goes through `tools/ocr_pdf.py`, the same path as `UNSUPPORTED_SCAN`. Any page still empty after OCR stays flagged.
- **TOC pages:** a page is flagged TOC when ≥ 30% of its lines match `\.{5,}\s*\d+$`. TOC pages are left out of retrieval and absence search.

### 4.2 `claims.py`
- Import `substantive_blocks`, `citation_units`, `SOURCE_RE`, `FIELD_LABEL_RE` and `ABSENCE_RE` from `tools/lint_sap.py`. Don't fork them.
- **Citation parser:** split the citation on `;`. In each part, read pages only after `page(s)`. A range ending in `reviewed` is a sweep. Unit tests come from pilot bugs: `Sections 1.1–1.3, pages 10–12` → {10, 11, 12}, and `Section 2.1, page 12; SAP …, pages 60–62` → {12, 60–62}.
- Record `outcome_block` (the nearest `**Outcome: …**` line) for the self-contradiction trigger.

### 4.3 `atomize.py`
- One call per SAP. Input: the claims. Output: `list[Fact]` via structured output.
- **Rule:** every fact must be a span or paraphrase of its claim, with no new content. A check confirms that each fact's numbers and named entities appear in its parent claim.
- Cache by `sha(md)`.

### 4.4 `retrieve.py`
- **Query:** heading + field + fact text.
- **Scores**, combined by RRF(60):
  - BM25 over pages (`rank_bm25`);
  - dense, with OpenAI embeddings on 250-word chunks every 200 words, page score = best chunk, cached;
  - number match, after normalization (`1,000` → 1000, x% → x/100); whole numbers ≤ 10 are dropped.
- **Fixes from the pilot:**
  - ties get their average rank;
  - pages with a number score of 0 are left out of that ranking;
  - IDF is fitted on PDF chunks only;
  - TOC pages are excluded.

### 4.5 `coverage.py`
- `Recall@k = mean 1[S ∩ Top_k ≠ ∅]` over facts with specific cited pages.
- Reported with:
  - the random-pick baseline `1 − C(n−|S|,k)/C(n,k)`;
  - a split by |S| ∈ {1, 2, 3–4, 5+};
  - MRR for |S| = 1.
- **Choosing k:** after P3, the smallest k with Recall@k ≥ 0.95, computed on facts whose page was judge-confirmed. Until then, k = 5.
- **Perturbation check:** it must change little when claims are corrupted, which documents that it is not an accuracy metric.

### 4.6 `triggers.py` (reason codes; any one puts the fact in T)

| Code | Rule |
|---|---|
| `NUM_MISS` | A non-trivial number in the fact is not on any cited page |
| `POLARITY` | ≥/≤, upper/lower, one-/two-sided, superiority/non-inferiority/equivalence or arm (TLH/TAH etc.) in the fact disagrees with the best passage on its cited pages |
| `COVERAGE_MISS` | S ∩ Top_k = ∅ |
| `SWEEP_ONLY` | The only citation is a page sweep |
| `ABSENCE_HITS` | Absence fact with at least one candidate page |
| `SELF_CONTRA` | An absence fact whose own `outcome_block` (or any field, for `FIELD_TOPICS`) states the item. No PDF needed |
| `LOW_OVERLAP` | Best-passage BM25 on the cited pages is in the lowest 10% for the document |
| `COMPOUND` | The parent claim produced more than 3 facts |
| `CRITICAL` | Section ∈ {2.2, 2.3, 2.5, 3.3, 5.1, 5.3, 5.4} |

The trigger set is **frozen at the end of P2**. Using LACE to tune it and then report
on it would make the results look better than they are; R∩U measures what the triggers
miss.

### 4.7 `sample.py`
Facts are ordered by section, then by position. With `u ~ U(0,1)` drawn from the seed,
fact *k* is selected if `⌊(k+1)·0.3 + u⌋ > ⌊k·0.3 + u⌋`. That gives π = 0.3 exactly.
**Don't** draw ⌈0.3·N_h⌉ per section: on LACE that takes 39% instead of 30%.

### 4.8 `packets.py`
Evidence for a fact = the top 3 passages (150 words, every 100) from the cited pages ∪
Top_k, **plus at least one passage from every cited page** (pilot C90 failed without
this). Passages are deduplicated across the document. Facts that share a section are
judged in one call (about 4–8 facts per call) with the union of their passages.

### 4.9 `judge.py`
- Output schema per fact: `{verdict, quote, quote_page, rationale ≤ 30 words}`.
- **Quote check:** after normalizing whitespace and hyphenation, the quote must be a substring of `pages[quote_page]`, and `quote_page` must be one of the pages shown. If it fails, re-ask once; if it fails again, the verdict is `not_found` and the fact goes to manual review.
- **Fixed checks:** `NUM_MISS` and `POLARITY` are re-checked against the quote. If the fixed check disagrees with a "supported" verdict, the override is recorded and the fact goes to review.
- Temperature 0. Log `prompt_sha`, model and token counts to `_cost.tsv`.

### 4.10 `absence.py`
- **Candidates** C(item) = pages matching the term list for the heading in `lexicon.yaml` ∪ pages with BM25 > τ ∪ the dense top 5 for the item description.
- No candidates and no unsearchable pages → `consistent`, with no LLM call. No candidates but some unsearchable pages → `unverifiable`.
- Otherwise the judge is asked "Does any passage specify ⟨item⟩? Quote it."
- `lexicon.yaml` is written per heading **before** looking at any QC output. It must include derived-variable terms (`change from baseline`, `between baseline`, `calculated`, `unit cost`), which was the pilot's miss on C96.
- **τ** is set in P3 so that recall ≈ 0.99 on facts that are present and confirmed.

### 4.11 `metrics.py`
- Per document:
  - `acc_T`, `acc_R`, and `miss = errors(R∩U)/|R∩U|`, each with a Clopper–Pearson 95% CI;
  - the lower-variance option `(|T|·acc_T + |U|·acc_{R∩U})/N`;
  - citation precision (cited pages that support the fact / cited pages);
  - absence-error rate.
- Pooled: a binomial GLMM with a random intercept per document, plus results by section.
- Strict vs lenient scoring (whether `minor` counts as an error) is reported both ways.

---

## 5. Phases and exit criteria

| Phase | Work | Exit criterion |
|---|---|---|
| **P0** Setup | Add `openai`, `pydantic`, `rank_bm25`, `scipy`, `statsmodels` to `pyproject.toml`; config file for the judge and embedding models | `run_qc.py --doc <LACE>` runs end to end with stubs |
| **P1** Deterministic layer | `pages`, `claims`, `retrieve`, `coverage`, `triggers` (all except `LOW_OVERLAP` calibration), `sample` | LACE: 111 claims, citation unit tests pass, Recall@k and the perturbation check reproduce, **no LLM tokens** |
| **P2** Model layer | `atomize`, `packets`, `judge`, `absence`, `metrics`; freeze triggers and `lexicon.yaml` | LACE: all quotes verify; C96 flagged by `SELF_CONTRA` and judged contradicted; C56 flagged |
| **P3** Validate the judge (**gate**) | `plant.py`: about 100 corrupted facts from 5 documents (numbers ×{0.6, 0.8, 1.25, 1.5}, arm swap, ≥↔≤, ITT→PP, present→"Not reported"), mixed blind with the originals. Gold set: about 100 facts labeled by hand, stratified by section | Proposed thresholds: detection ≥ 0.90, false alarm ≤ 0.05, κ ≥ 0.70. Then set k and τ |
| **P4** Batch | Run all 100 documents in `pair-sample-1`; write `_summary.tsv`, `_coverage.tsv`, `_cost.tsv`; add a pooled results section to `docs/qc-pipeline.html` | Pooled `acc_R` and miss rate with CIs; cost per document |
| **P5** Optional | Full-PDF route for PDFs of ≤ 100 pages; compare verdict agreement with the retrieval route on 20 documents | Use it only if agreement ≥ 0.95 and it is cheaper |

---

## 6. Tests (`qc/tests/`)
- **Citation parser:** section numbers vs pages, multiple `;` parts, en dash vs hyphen, sweep-only citations, `pages 8–9` + `page 16`.
- **Number normalizer:** `.05` = `0.05` = `5%`, `1,000`, mixed `97.5%`.
- **RRF:** tied ranks are averaged; a page with all-zero scores is not ranked ahead because of its page index.
- **Sampler:** |R| = round(0.3·N) ± 1; each fact's empirical π = 0.3 over 10k seeds.
- **Quote checker:** hyphenation across a line break, ligatures, extra whitespace.
- **Regression fixture:** LACE (`10.1001_jama.2017.2068`) with its pilot verdicts.

---

## 7. Cost (estimate, to be checked in P2)
Per document:
- atomizing: about 5k tokens in and 6k out;
- judging: about 16k tokens in for |T ∪ R| ≈ 54 facts (LACE pilot);
- embeddings: about 30k tokens, cached.

That's on the order of 2–4M tokens for 100 documents. The tail is long: PDFs run up to 1,228 pages, but the judge's input grows with the number of facts, not pages.

---

## 8. Risks and open questions
1. **Judge model.** Under the OpenAI-only standard, the judge must be a third model. The team needs to choose which.
2. **Atomizing moves the denominator.** Report fact-level and claim-level numbers side by side. Check atomizer agreement (a second run, Jaccard on facts) on 10 documents.
3. **Triggers tuned on one document.** Freezing them in P2 and measuring R∩U addresses this, but the critical-section trigger alone may put about 40% of facts in T. Watch the cost.
4. **Extractor change.** The pilot used Poppler's `pdftotext`; v1 uses pypdf (D5). Re-run the LACE coverage numbers before quoting them.
5. **Lexicon recall** is unknown until P3. Absence verdicts stay provisional until then.
6. **Disagreements inside the protocol** (equivalence vs non-inferiority, two DFS definitions). A fact that matches one version should not be scored contradicted by the other. The judge prompt says: supported if *any* cited page supports it, and note the conflict.
