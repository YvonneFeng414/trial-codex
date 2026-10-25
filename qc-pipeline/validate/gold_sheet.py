#!/usr/bin/env python3
"""Human gold labels for validating the judge (P3).

  export  draw ~100 judged facts across documents, stratified by SAP section, into a CSV
          for blind labelling (the judge's verdicts go to a separate key file)
  score   read the filled CSV back and report agreement with the judge: Cohen's kappa on
          error vs correct, and on the full verdict

Labels: supported, contradicted, not_found (present facts); consistent, contradicted,
unverifiable (absence facts). Put "1" in `minor` for a minor contradiction.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SHEET_FIELDS = ("gold_id", "doc_id", "fact_id", "section", "field", "outcome_block", "status", "text",
                "item", "cited_pages", "evidence_pages", "pdf", "human_verdict", "minor", "notes")
LABELS = {"present": {"supported", "contradicted", "not_found"},
          "absent": {"consistent", "contradicted", "unverifiable"}}


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def kappa(a: list[str], b: list[str]) -> float | None:
    if not a:
        return None
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[label] * cb[label] for label in set(a) | set(b)) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def export(out: Path, n: int, seed: int) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    strata: dict[str, list[tuple[dict, dict, str]]] = {}
    for verdict_path in sorted(out.glob("*/*.verdicts.jsonl")):
        stem = verdict_path.name[: -len(".verdicts.jsonl")]
        facts = {f["fact_id"]: f for f in _read_jsonl(verdict_path.with_name(f"{stem}.facts.jsonl"))}
        run = json.loads(verdict_path.with_name(f"{stem}.run.json").read_text(encoding="utf-8"))
        for verdict in _read_jsonl(verdict_path):
            fact = facts[verdict["fact_id"]]
            strata.setdefault(fact["section"], []).append((fact, verdict, run["pdf"]))
    total = sum(len(v) for v in strata.values())
    if not total:
        return [], []
    # Proportional allocation with at least one per section, then trimmed to n.
    chosen: list[tuple[dict, dict, str]] = []
    for section in sorted(strata):
        members = strata[section]
        rng.shuffle(members)
        chosen += members[: max(1, round(n * len(members) / total))]
    rng.shuffle(chosen)
    chosen = chosen[:n]
    sheet, key = [], []
    for number, (fact, verdict, pdf) in enumerate(chosen):
        gold_id = f"G{number:04d}"
        sheet.append({
            "gold_id": gold_id, "doc_id": fact["doc_id"], "fact_id": fact["fact_id"], "section": fact["section"],
            "field": fact["field"], "outcome_block": fact.get("outcome_block") or "", "status": fact["status"],
            "text": fact["text"], "item": fact["item"], "cited_pages": " ".join(map(str, fact["cited_pages"])),
            "evidence_pages": " ".join(map(str, verdict.get("evidence_pages") or [])), "pdf": pdf,
            "human_verdict": "", "minor": "", "notes": "",
        })
        key.append({"gold_id": gold_id, "verdict": verdict["verdict"], "minor": verdict.get("minor", False)})
    return sheet, key


def score(sheet: list[dict], key: dict[str, dict]) -> dict:
    from qc.metrics import is_error

    human, judge, human_err, judge_err, invalid = [], [], [], [], []
    for row in sheet:
        label = row["human_verdict"].strip().lower()
        if not label:
            continue
        if label not in LABELS[row["status"]]:
            invalid.append(row["gold_id"])
            continue
        verdict = key[row["gold_id"]]
        human.append(label)
        judge.append(verdict["verdict"])
        h = is_error({"verdict": label, "minor": row["minor"].strip() == "1"}, strict=True)
        j = is_error(verdict, strict=True)
        if h is not None and j is not None:
            human_err.append(str(h))
            judge_err.append(str(j))
    agree = sum(h == j for h, j in zip(human, judge))
    return {
        "labelled": len(human), "invalid_labels": invalid,
        "agreement": agree / len(human) if human else None,
        "kappa_verdict": kappa(human, judge), "kappa_error": kappa(human_err, judge_err),
        "gate_kappa>=0.70": (kappa(human_err, judge_err) or 0) >= 0.70,
        "confusion": Counter(f"{h}->{j}" for h, j in zip(human, judge)),
    }


def main() -> int:
    from qc import QC_ROOT

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("export", "score"))
    parser.add_argument("--out", type=Path, default=QC_ROOT / "out")
    parser.add_argument("--sheet", type=Path, default=None, help="default: <out>/_gold/gold_sheet.csv")
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    gold_dir = args.out / "_gold"
    sheet_path = args.sheet or gold_dir / "gold_sheet.csv"
    key_path = gold_dir / "gold_key.jsonl"

    if args.command == "export":
        sheet, key = export(args.out, args.n, args.seed)
        if not sheet:
            print("error: no judged documents under --out", file=sys.stderr)
            return 1
        gold_dir.mkdir(parents=True, exist_ok=True)
        if sheet_path.exists():
            print(f"error: {sheet_path} exists; move it aside before exporting a new sheet", file=sys.stderr)
            return 1
        with open(sheet_path, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=SHEET_FIELDS)
            writer.writeheader()
            writer.writerows(sheet)
        with open(key_path, "w", encoding="utf-8") as stream:
            for row in key:
                stream.write(json.dumps(row) + "\n")
        print(f"exported {len(sheet)} facts -> {sheet_path} (judge key kept apart in {key_path})")
        return 0

    with open(sheet_path, newline="", encoding="utf-8") as stream:
        sheet = list(csv.DictReader(stream))
    key = {row["gold_id"]: row for row in _read_jsonl(key_path)}
    result = score(sheet, key)
    (gold_dir / "gold_score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
