#!/usr/bin/env python3
"""SAP faithfulness QC for one document or a batch over pairs.csv.

  run_qc.py --doc Benchmark_expr_samples/pair-sample-1/10.1001_jama.2017.2068 --stage deterministic
  run_qc.py --pairs Benchmark_expr_samples/pair-sample-1/pairs.csv --backend openai
  run_qc.py pool        # pooled metrics over every judged document in --out
  run_qc.py calibrate   # P3: k from judge-confirmed facts, and τ for absence search

Stages: `deterministic` (P1: pages, claims, rule atomizer, retrieval, coverage, triggers,
sample; no model tokens) and `full` (adds atomizer, judge and metrics). `--backend stub`
runs the full stage end to end with deterministic stand-ins for every model call.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qc import QC_ROOT, REPO_ROOT
from qc import absence, atomize, claims as claims_mod, coverage, metrics, pages as pages_mod
from qc import sample, triggers
from qc.config import ConfigError, check_model, judge_independent, load as load_config
from qc.judge import Judge
from qc.llm import CostLog, OpenAIBackend, require_api_key
from qc.packets import PassageIndex, absence_evidence, group_calls, item_query, present_evidence
from qc.retrieve import PageIndex, retrieve
from qc.text import sha

SUMMARY_FIELDS = (
    "doc_id", "status", "stage", "backend", "judge_model", "judge_independent", "dense", "n_pages", "toc_pages",
    "unsearchable_pages", "claims", "unparsed_citations", "facts", "absent_facts", "T", "R", "RU", "judged",
    "acc_T", "acc_T_lo", "acc_T_hi", "acc_R", "acc_R_lo", "acc_R_hi",
    "acc_R_lenient", "miss", "miss_n", "miss_hi", "acc_lowvar", "citation_precision",
    "absence_error_rate", "needs_review", "jev_mode", "jev_accepted", "llm_judged", "recall5_facts", "recall5_claims",
    "input_tokens", "output_tokens", "seconds", "error",
)
COVERAGE_FIELDS = ("doc_id", "unit", "n", "k", "recall@1", "recall@3", "recall@5", "recall@10",
                   "baseline@5", "mrr_single", "unrankable", "perturbed_n", "perturbed_recall@5", "original_recall@5")
CSV_FIELDS = ("fact_id", "claim_id", "section", "field", "outcome_block", "status", "text", "item",
              "cited_pages", "sweep", "topk", "cited_rank", "triggers", "in_T", "in_R", "stratum",
              "verdict", "minor", "quote", "quote_page", "quote_verified", "supporting_pages",
              "override", "needs_review", "rationale")


@dataclass
class Pair:
    doc_id: str
    doi_dir: str
    stem: str
    md: Path
    pdf: Path


def pairs_from_csv(path: Path, limit: int | None) -> list[Pair]:
    base = path.parent
    out = []
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            md, pdf = (base / row["md_path"]).resolve(), (base / row["pdf_path"]).resolve()
            out.append(Pair(f"{row['doi_dir']}/{md.stem}", row["doi_dir"], md.stem, md, pdf))
    return out[:limit] if limit else out


def pair_from_dir(path: Path, stem: str | None) -> Pair:
    path = path.resolve()
    mds = sorted(path.glob("*.md"))
    if stem:
        mds = [m for m in mds if m.stem == stem]
    if len(mds) != 1:
        raise SystemExit(f"error: expected one SAP .md in {path} (found {len(mds)}); use --stem")
    md = mds[0]
    pdf = md.with_suffix(".pdf")
    if not pdf.is_file():
        raise SystemExit(f"error: no PDF next to {md}")
    return Pair(f"{path.name}/{md.stem}", path.name, md.stem, md, pdf)


def write_jsonl(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def stratum(fact: dict) -> str:
    return {(True, True): "T∩R", (True, False): "T only", (False, True): "R∩U", (False, False): "U only"}[
        (fact["in_T"], fact["in_R"])
    ]


class Document:
    """Everything one document's stages share: pages, claims, facts, indexes, judge."""

    def __init__(self, pair: Pair, config: dict, args, cost: CostLog):
        self.pair, self.config, self.args = pair, config, args
        self.workdir = args.out / pair.doi_dir
        self.cache = self.workdir / "cache"
        self.cost = cost
        self.backend = OpenAIBackend(config, cost) if args.backend == "openai" else None
        self.pages, self.page_meta = pages_mod.load(pair.pdf, self.workdir, pair.stem, ocr=getattr(args, "ocr", False))
        self.markdown = pair.md.read_text(encoding="utf-8")
        self.claims = claims_mod.parse(self.markdown, pair.doc_id, max_page=len(self.pages))
        dense_backend = self.backend
        if args.dense == "openai" and dense_backend is None:
            dense_backend = OpenAIBackend(config, cost)
        self.index = PageIndex(self.pages, config, dense=args.dense, backend=dense_backend,
                               cache_dir=self.cache, doc_id=pair.doc_id)
        self.passages = PassageIndex(self.pages, config)
        self.lexicon = absence.load_lexicon(args.lexicon)
        self.arms = triggers.arm_labels(self.claims)

    def atomize(self, use_model: bool) -> list[dict]:
        backend = self.backend if use_model else None
        return atomize.atomize(self.claims, backend, self.pair.doc_id, self.cache, sha(self.markdown))

    def absence_candidates(self, facts: list[dict]) -> dict[str, dict]:
        absent = [f for f in facts if f["status"] == "absent"]
        dense = self.index.dense_scores([item_query(f) for f in absent]) if absent else []
        return {
            f["fact_id"]: absence.candidates(f, self.pages, self.index, self.lexicon, self.config, d)
            for f, d in zip(absent, dense)
        }

    def front_end(self):
        """JEV in front of the LLM judge (config jev.enabled); a stand-in for stub runs."""
        if not self.config["jev"]["enabled"]:
            return None
        if self.backend is None:
            from qc.jev import StubJev

            return StubJev()
        from qc.jev import JevFrontEnd

        return JevFrontEnd(self.config, self.cost, self.pair.doc_id)

    def judge(self, facts: list[dict], retrievals: dict[str, dict], codes: dict[str, list[str]], found: dict[str, dict]) -> dict[str, dict]:
        judge = Judge(self.backend, self.pages, self.passages, self.config, self.arms, self.pair.doc_id,
                      front=self.front_end())
        verdicts: dict[str, dict] = {}
        to_judge = []
        for fact in facts:
            if fact["status"] == "absent":
                auto = absence.auto_verdict(found[fact["fact_id"]], self.pages)
                if auto:
                    verdicts[fact["fact_id"]] = {
                        "verdict": auto, "raw_verdict": None, "minor": False, "quote": "", "quote_page": None,
                        "quote_verified": False, "supporting_pages": [], "rationale": "no candidate pages",
                        "override": None, "needs_review": False, "retried": False, "judge_model": None,
                        "prompt_sha": None, "evidence_pages": [],
                    }
                    continue
            to_judge.append(fact)
        for call in group_calls(to_judge, self.config):
            passage_ids: list[int] = []
            for fact in call:
                if fact["status"] == "absent":
                    passage_ids += absence_evidence(fact, found[fact["fact_id"]]["pages"], self.passages, self.config,
                                                    absence.terms_for(fact, self.lexicon))
                else:
                    passage_ids += present_evidence(fact, retrievals[fact["fact_id"]], codes[fact["fact_id"]], self.passages, self.config)
            passage_ids = list(dict.fromkeys(passage_ids))
            if not passage_ids:
                for fact in call:
                    verdicts[fact["fact_id"]] = {"verdict": "not_found" if fact["status"] == "present" else "unverifiable",
                                                 "raw_verdict": None, "minor": False, "quote": "", "quote_page": None,
                                                 "quote_verified": False, "supporting_pages": [], "rationale": "no evidence passages",
                                                 "override": None, "needs_review": True, "retried": False,
                                                 "judge_model": judge.model, "prompt_sha": None, "evidence_pages": []}
                continue
            verdicts.update(judge.run_call(call, passage_ids, codes))
        return verdicts


def perturbation(doc: Document, facts: list[dict], seed: int) -> dict:
    """Recall@5 on corrupted copies of the facts vs the originals (D1)."""
    import random

    from validate.plant import corrupt

    rng = random.Random(seed)
    originals, corrupted = [], []
    for fact in facts:
        if fact["status"] != "present" or not fact["cited_pages"]:
            continue
        for kind in ("arm_swap", "direction_flip", "scale"):
            bad = corrupt(fact, kind, rng, doc.arms)
            if bad is not None:
                originals.append(fact)
                corrupted.append(bad)
                break
    if not corrupted:
        return {"n": 0}
    k = doc.config["retrieval"]["k"]
    before = retrieve(originals, doc.index, "fact_id")
    after = retrieve(corrupted, doc.index, "fact_id")
    hit = lambda rows: sum(r["cited_rank"] is not None and r["cited_rank"] <= k for r in rows) / len(rows)  # noqa: E731
    return {"n": len(corrupted), "original_recall": hit(before), "perturbed_recall": hit(after)}


def run_document(pair: Pair, config: dict, args, cost: CostLog) -> tuple[dict, list[dict]]:
    started = time.time()
    full = args.stage == "full"
    doc = Document(pair, config, args, cost)
    work = doc.workdir
    stem = pair.stem

    use_model_atoms = full and args.atomizer == "model" and doc.backend is not None
    facts = doc.atomize(use_model_atoms)
    write_jsonl(work / f"{stem}.claims.jsonl", doc.claims)

    fact_retrieval = retrieve(facts, doc.index, "fact_id")
    claim_retrieval = retrieve(doc.claims, doc.index, "claim_id")
    for record in fact_retrieval:
        record["id"] = record["fact_id"]
    for record in claim_retrieval:
        record["id"] = str(record["claim_id"])
    retrievals = {r["fact_id"]: r for r in fact_retrieval}
    write_jsonl(work / f"{stem}.retrieval.jsonl", fact_retrieval)
    write_jsonl(work / f"{stem}.retrieval_claims.jsonl", claim_retrieval)

    ks, k = config["retrieval"]["k_report"], config["retrieval"]["k"]
    searchable_pages = {p["page"] for p in pages_mod.searchable(doc.pages)}
    cover = {
        "facts": coverage.report(fact_retrieval, {f["fact_id"]: f["cited_pages"] for f in facts}, searchable_pages, ks, k),
        "claims": coverage.report(claim_retrieval, {str(c["claim_id"]): c["cited_pages"] for c in doc.claims}, searchable_pages, ks, k),
        "dense": doc.index.dense_name,
    }
    if args.perturb:
        cover["perturbation"] = perturbation(doc, facts, config["sampling"]["seed"])
    (work / f"{stem}.coverage.json").write_text(json.dumps(cover, indent=2), encoding="utf-8")

    found = doc.absence_candidates(facts)
    fired = triggers.evaluate(facts, fact_retrieval, doc.claims, doc.pages, doc.passages, found, doc.lexicon, config)
    in_R, u = sample.draw(facts, config["sampling"]["pi"], config["sampling"]["seed"])
    for fact in facts:
        fact["triggers"] = fired[fact["fact_id"]]["codes"]
        fact["trigger_detail"] = fired[fact["fact_id"]]["detail"]
        fact["in_T"] = bool(fact["triggers"])
        fact["in_R"] = fact["fact_id"] in in_R
        if fact["fact_id"] in found:
            fact["absence_candidates"] = found[fact["fact_id"]]

    verdicts: dict[str, dict] = {}
    if full:
        selected = [f for f in facts if f["in_T"] or f["in_R"]]
        codes = {f["fact_id"]: f["triggers"] for f in facts}
        verdicts = doc.judge(selected, retrievals, codes, found)
        rows = []
        for fact in facts:
            if fact["fact_id"] in verdicts:
                rows.append({"fact_id": fact["fact_id"], "in_T": fact["in_T"], "in_R": fact["in_R"],
                             "triggers": fact["triggers"], **verdicts[fact["fact_id"]]})
        write_jsonl(work / f"{stem}.verdicts.jsonl", rows)
    write_jsonl(work / f"{stem}.facts.jsonl", facts)

    result = {"strict": metrics.doc_metrics(facts, verdicts, strict=True),
              "lenient": metrics.doc_metrics(facts, verdicts, strict=False)} if full else {}
    if full:
        (work / f"{stem}.metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    with open(work / f"{stem}.qc.csv", "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for fact in facts:
            verdict = verdicts.get(fact["fact_id"], {})
            retrieval = retrievals[fact["fact_id"]]
            writer.writerow({
                **{key: fact.get(key) for key in ("fact_id", "claim_id", "section", "field", "outcome_block", "status", "text", "item")},
                "cited_pages": " ".join(map(str, fact["cited_pages"])),
                "sweep": " ".join(f"{a}-{b}" for a, b in fact["sweep"]),
                "topk": " ".join(map(str, retrieval["topk"])),
                "cited_rank": retrieval["cited_rank"],
                "triggers": " ".join(fact["triggers"]),
                "in_T": int(fact["in_T"]), "in_R": int(fact["in_R"]), "stratum": stratum(fact),
                "verdict": verdict.get("verdict", ""), "minor": int(bool(verdict.get("minor"))) if verdict else "",
                "quote": verdict.get("quote", ""), "quote_page": verdict.get("quote_page") or "",
                "quote_verified": int(bool(verdict.get("quote_verified"))) if verdict else "",
                "supporting_pages": " ".join(map(str, verdict.get("supporting_pages") or [])),
                "override": " ".join(verdict.get("override") or []),
                "needs_review": int(bool(verdict.get("needs_review"))) if verdict else "",
                "rationale": verdict.get("rationale", ""),
            })

    run_info = {
        "doc_id": pair.doc_id, "md": str(pair.md), "pdf": str(pair.pdf), "stage": args.stage,
        "backend": args.backend, "atomizer": "model" if use_model_atoms else "rule", "dense": doc.index.dense_name,
        "judge_independent": judge_independent(config) if doc.backend else None,
        "jev": config["jev"] if config["jev"]["enabled"] else None,
        "seed": config["sampling"]["seed"], "u": u, "pi": config["sampling"]["pi"],
        "triggers_version": triggers.TRIGGERS_VERSION, "lexicon_version": doc.lexicon.get("version"),
        "lexicon_sha": sha(json.dumps(doc.lexicon, sort_keys=True)), "md_sha": sha(doc.markdown),
        "page_meta": {k: v for k, v in doc.page_meta.items() if k != "log"}, "page_log": doc.page_meta.get("log"),
        "config": config, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (work / f"{stem}.run.json").write_text(json.dumps(run_info, indent=2), encoding="utf-8")

    strict = result.get("strict", {})
    lenient = result.get("lenient", {})
    doc_cost = [r for r in cost.rows if r.get("doc_id") == pair.doc_id]
    row = {
        "doc_id": pair.doc_id, "status": "ok", "stage": args.stage, "backend": args.backend,
        "judge_model": (doc.backend.config["models"]["judge"] if doc.backend else ("stub" if full else "")),
        "judge_independent": (judge_independent(config) if doc.backend else ""),
        "dense": doc.index.dense_name, "n_pages": len(doc.pages),
        "toc_pages": len(doc.page_meta["toc"]), "unsearchable_pages": len(doc.page_meta["unsearchable"]),
        "claims": len(doc.claims), "unparsed_citations": sum(c["citation_unparsed"] for c in doc.claims),
        "facts": len(facts), "absent_facts": sum(f["status"] == "absent" for f in facts),
        "T": sum(f["in_T"] for f in facts), "R": sum(f["in_R"] for f in facts),
        "RU": sum(f["in_R"] and not f["in_T"] for f in facts), "judged": len(verdicts),
        "recall5_facts": cover["facts"]["recall"].get(5), "recall5_claims": cover["claims"]["recall"].get(5),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in doc_cost),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in doc_cost),
        "seconds": round(time.time() - started, 1),
    }
    if full:
        row.update({
            "acc_T": strict["acc_T"]["acc"], "acc_T_lo": strict["acc_T"]["ci"][0], "acc_T_hi": strict["acc_T"]["ci"][1],
            "acc_R": strict["acc_R"]["acc"], "acc_R_lo": strict["acc_R"]["ci"][0], "acc_R_hi": strict["acc_R"]["ci"][1],
            "acc_R_lenient": lenient["acc_R"]["acc"], "miss": strict["miss"]["rate"], "miss_n": strict["miss"]["n"],
            "miss_hi": strict["miss"]["ci"][1], "acc_lowvar": strict["acc_lowvar"],
            "citation_precision": strict["citation_precision"], "absence_error_rate": strict["absence_error_rate"],
            "needs_review": strict["needs_review"],
            "jev_mode": config["jev"]["mode"] if config["jev"]["enabled"] else "off",
            "jev_accepted": sum(v.get("judge_stage") == "jev" for v in verdicts.values()),
            "llm_judged": sum(v.get("judge_stage") == "llm" for v in verdicts.values()),
        })
    cov_rows = []
    for unit in ("facts", "claims"):
        report = cover[unit]
        cov_row = {"doc_id": pair.doc_id, "unit": unit, "n": report["n"], "k": k,
                   "baseline@5": report["baseline"].get(5), "mrr_single": report.get("mrr_single"),
                   "unrankable": report.get("unrankable")}
        for kk in (1, 3, 5, 10):
            cov_row[f"recall@{kk}"] = report["recall"].get(kk)
        if unit == "facts" and "perturbation" in cover and cover["perturbation"].get("n"):
            cov_row.update({"perturbed_n": cover["perturbation"]["n"],
                            "perturbed_recall@5": cover["perturbation"]["perturbed_recall"],
                            "original_recall@5": cover["perturbation"]["original_recall"]})
        cov_rows.append(cov_row)
    return row, cov_rows


def _fmt(value):
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.4f}"
    return "" if value is None else value


def upsert_tsv(path: Path, fields: tuple[str, ...], rows: list[dict], key: tuple[str, ...]) -> None:
    """Replace this run's documents in a summary table, keeping every other document's rows."""
    existing: list[dict] = []
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as stream:
            existing = list(csv.DictReader(stream, delimiter="\t"))
    replaced = {tuple(str(r.get(k, "")) for k in key) for r in rows}
    keep = [r for r in existing if tuple(r.get(k, "") for k in key) not in replaced]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in keep + [{k: _fmt(v) for k, v in r.items()} for r in rows]:
            writer.writerow(row)


def judge_planted(items: list[dict], args) -> dict[str, dict]:
    """Judge plant.py's blind set with the same retrieval, packets and judge as a QC run."""
    config = load_config(args.config if hasattr(args, "config") else None)
    args.lexicon = getattr(args, "lexicon", None)
    args.dense = args.dense or ("openai" if args.backend == "openai" else "tfidf")
    cost = CostLog(args.out / "_cost.tsv")
    if args.backend == "openai":
        check_model(config, "judge")
    if args.backend == "openai" or args.dense == "openai":
        require_api_key()
    pairs = {p.doi_dir: p for p in pairs_from_csv(args.pairs, None)}
    verdicts: dict[str, dict] = {}
    by_doc: dict[str, list[dict]] = {}
    for item in items:
        by_doc.setdefault(item["doc_dir"], []).append(item["fact"])
    for doc_dir, facts in by_doc.items():
        doc = Document(pairs[doc_dir], config, args, cost)
        retrievals = {r["fact_id"]: r for r in retrieve(facts, doc.index, "fact_id")}
        found = doc.absence_candidates(facts)
        codes = {f["fact_id"]: [] for f in facts}
        verdicts.update(doc.judge(facts, retrievals, codes, found))
    return verdicts


def cmd_pool(args) -> int:
    rows, docs, not_independent = [], 0, 0
    for verdict_path in sorted(args.out.glob("*/*.verdicts.jsonl")):
        facts = {f["fact_id"]: f for f in read_jsonl(verdict_path.with_name(verdict_path.name.replace(".verdicts.", ".facts.")))}
        docs += 1
        run_path = verdict_path.with_name(verdict_path.name.replace(".verdicts.jsonl", ".run.json"))
        if run_path.is_file() and json.loads(run_path.read_text(encoding="utf-8")).get("judge_independent") is False:
            not_independent += 1
        for verdict in read_jsonl(verdict_path):
            fact = facts[verdict["fact_id"]]
            if fact["in_R"]:
                rows.append({"doc_id": fact["doc_id"], "section": fact["section"], "verdict": verdict})
    result = {"documents": docs, "judge_not_independent": not_independent,
              "strict": metrics.pooled(rows, True), "lenient": metrics.pooled(rows, False)}
    (args.out / "_pooled.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: (v if k in ("documents", "judge_not_independent") else {kk: v.get(kk) for kk in ("n", "docs", "pooled_proportion", "glmm")}) for k, v in result.items()}, indent=2))
    return 0


def cmd_calibrate(args) -> int:
    """P3: k = smallest with Recall@k >= 0.95 on judge-confirmed facts; τ for absence."""
    config = load_config(args.config)
    records, confirmed = [], set()
    for verdict_path in sorted(args.out.glob("*/*.verdicts.jsonl")):
        stem = verdict_path.name[: -len(".verdicts.jsonl")]
        for verdict in read_jsonl(verdict_path):
            if verdict["verdict"] == "supported" and verdict.get("quote_verified") and verdict.get("supporting_pages"):
                confirmed.add(f"{verdict_path.parent.name}/{verdict['fact_id']}")
        for record in read_jsonl(verdict_path.with_name(f"{stem}.retrieval.jsonl")):
            records.append(dict(record, id=f"{verdict_path.parent.name}/{record['fact_id']}"))
    k = coverage.choose_k(records, confirmed)

    # JEV acceptance threshold: facts JEV and the LLM both judged (every fact in shadow mode;
    # only escalations in cascade mode, which biases the table towards hard cases).
    from qc.jev import calibration_table, choose_tau

    pairs, modes = [], set()
    for verdict_path in sorted(args.out.glob("*/*.verdicts.jsonl")):
        status = {f["fact_id"]: f["status"] for f in read_jsonl(verdict_path.with_name(verdict_path.name.replace(".verdicts.", ".facts.")))}
        run_path = verdict_path.with_name(verdict_path.name.replace(".verdicts.jsonl", ".run.json"))
        if run_path.is_file():
            modes.add((json.loads(run_path.read_text(encoding="utf-8")).get("jev") or {}).get("mode"))
        for verdict in read_jsonl(verdict_path):
            if verdict.get("judge_stage") == "llm" and verdict.get("jev") and verdict["verdict"] != "unverifiable":
                pairs.append((verdict["jev"], status[verdict["fact_id"]], verdict["verdict"]))
    if pairs:
        table = calibration_table(pairs)
        print(json.dumps({"jev_pairs": len(pairs), "jev_modes": sorted(m for m in modes if m),
                          "jev_accept_confidence": choose_tau(table, config["jev"]["max_disagreement"]),
                          "jev_table": table}, indent=2))
        if "shadow" not in modes:
            print("note: no shadow-mode runs; the JEV table only covers escalated facts. "
                  "Re-run with --jev shadow for an unbiased threshold.", file=sys.stderr)

    print(json.dumps({"confirmed_facts": len(confirmed), "k": k,
                      "tau": "run with --pairs to recompute BM25 scores per document" if not args.pairs else None}, indent=2))
    if args.pairs:
        taus = []
        args.dense, args.backend, args.lexicon = "none", "stub", None
        cost = CostLog(None)
        for pair in pairs_from_csv(args.pairs, None):
            verdict_path = args.out / pair.doi_dir / f"{pair.stem}.verdicts.jsonl"
            if not verdict_path.is_file():
                continue
            doc = Document(pair, config, args, cost)
            facts = [f for f in read_jsonl(verdict_path.with_name(f"{pair.stem}.facts.jsonl")) if f["status"] == "present"]
            verdicts = {v["fact_id"]: v for v in read_jsonl(verdict_path)}
            tau = absence.calibrate_tau(facts, verdicts, doc.index)
            if tau is not None:
                taus.append(tau)
        print(json.dumps({"tau_per_doc": taus, "tau": min(taus) if taus else None}, indent=2))
    return 0


def parse_args(argv: list[str]):
    command = argv[0] if argv and argv[0] in ("pool", "calibrate") else "run"
    if command != "run":
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=QC_ROOT / "out")
    parser.add_argument("--config", type=Path, default=None)
    if command == "calibrate":
        parser.add_argument("--pairs", type=Path, default=None)
    if command == "run":
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--doc", type=Path, help="directory holding one <stem>.md + <stem>.pdf pair")
        source.add_argument("--pairs", type=Path, help="pairs.csv (paths relative to its directory)")
        parser.add_argument("--stem", help="pick one pair when --doc holds several")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--stage", choices=("deterministic", "full"), default="full")
        parser.add_argument("--backend", choices=("openai", "stub"), default="openai")
        parser.add_argument("--atomizer", choices=("model", "rule"), default="model")
        parser.add_argument("--dense", choices=("openai", "tfidf", "none"), default=None,
                            help="default: openai with --backend openai, else tfidf (no tokens)")
        parser.add_argument("--lexicon", type=Path, default=None)
        parser.add_argument("--seed", type=int, default=None, help="override sampling.seed")
        parser.add_argument("--k", type=int, default=None, help="override retrieval.k")
        parser.add_argument("--perturb", action="store_true", help="also run the coverage perturbation check")
        parser.add_argument("--jev", choices=("cascade", "shadow", "off"), default=None,
                            help="override jev.mode (off disables the JEV front end)")
        parser.add_argument("--ocr", action="store_true",
                            help="OCR thin pages with tools/ocr_pdf.py (off: they stay flagged unsearchable)")
        parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    args.command = command
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "pool":
        return cmd_pool(args)
    if args.command == "calibrate":
        return cmd_calibrate(args)

    if args.stage == "deterministic":
        args.backend = "stub"  # no model calls at all in P1
    if args.dense is None:
        args.dense = "openai" if args.backend == "openai" else "tfidf"
    config = load_config(args.config, {"sampling.seed": args.seed, "retrieval.k": args.k})
    if args.jev:
        config["jev"]["enabled"] = args.jev != "off"
        if args.jev != "off":
            config["jev"]["mode"] = args.jev
    try:
        if args.backend == "openai":
            check_model(config, "judge")
            check_model(config, "atomizer")
        if args.backend == "openai" or args.dense == "openai":
            require_api_key()
        if args.backend == "openai" and args.stage == "full" and config["jev"]["enabled"]:
            from qc.jev import require_typesafe_key

            require_typesafe_key()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.backend == "openai" and not judge_independent(config):
        print(f"warning: judge {config['models']['judge']!r} is the generator or reviewer model; "
              "results are recorded as judge_independent=false (D6 overridden)", file=sys.stderr)

    pairs = pairs_from_csv(args.pairs, args.limit) if args.pairs else [pair_from_dir(args.doc, args.stem)]
    cost = CostLog(args.out / "_cost.tsv")
    summary, cover_rows = [], []
    for number, pair in enumerate(pairs, start=1):
        print(f"[{number}/{len(pairs)}] {pair.doc_id}", flush=True)
        try:
            row, cov = run_document(pair, config, args, cost)
            summary.append(row)
            cover_rows += cov
            shown = {k: _fmt(row.get(k)) for k in ("claims", "facts", "T", "R", "judged", "recall5_facts", "recall5_claims", "acc_T", "acc_R")}
            print("    " + "  ".join(f"{k}={v}" for k, v in shown.items() if v != ""), flush=True)
        except Exception as exc:
            if args.fail_fast:
                raise
            summary.append({"doc_id": pair.doc_id, "status": "error", "stage": args.stage, "error": f"{type(exc).__name__}: {exc}"})
            print(f"    error: {exc}", file=sys.stderr)
            (args.out / pair.doi_dir).mkdir(parents=True, exist_ok=True)
            (args.out / pair.doi_dir / f"{pair.stem}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    upsert_tsv(args.out / "_summary.tsv", SUMMARY_FIELDS, summary, ("doc_id",))
    upsert_tsv(args.out / "_coverage.tsv", COVERAGE_FIELDS, cover_rows, ("doc_id", "unit"))
    failed = sum(r["status"] == "error" for r in summary)
    print(f"done: {len(summary) - failed} ok, {failed} failed -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
