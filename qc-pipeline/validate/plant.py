#!/usr/bin/env python3
"""Planted errors for validating the judge (P3), and the corruptions the coverage
perturbation check reuses.

  make   corrupt ~100 facts from several processed documents and mix them, blind, with
         their originals:   plant.py make --docs DIR... --n 100 --seed 7
  judge  judge the blind set with the same retrieval, packets and judge as run_qc.py
  score  detection rate P(not supported | corrupted) and false-alarm rate
         P(not supported | original), per corruption kind

Corruptions: numbers x{0.6, 0.8, 1.25, 1.5}, arm swap, >= <-> <=, ITT -> PP, and a
reported item relabelled "Not reported in protocol".
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qc.text import normalize, numbers

FACTORS = (0.6, 0.8, 1.25, 1.5)
KINDS = ("scale", "arm_swap", "direction_flip", "itt_to_pp", "to_absent")
_NUMBER_SPAN_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\d])(\s*%)?")
_DIRECTION_SWAPS = (("≥", "≤"), (">=", "<="), ("at least", "at most"), ("no less than", "no more than"),
                    ("or more", "or less"), ("upper", "lower"))
_ITT_RE = re.compile(r"\bintention[- ]to[- ]treat\b|\bintent[- ]to[- ]treat\b|\bITT\b", re.IGNORECASE)


def _scaled(match: re.Match[str], factor: float) -> str:
    whole, decimals, pct = match.group(1), match.group(2) or "", match.group(3) or ""
    value = float((whole + decimals).replace(",", "")) * factor
    places = len(decimals) - 1 if decimals else 0
    text = f"{value:.{places}f}" if places else str(max(1, round(value)))
    if text == (whole + decimals).replace(",", ""):
        text = str(int(text) + 1) if not places else f"{value + 10 ** -places:.{places}f}"
    return text + pct


def scale_numbers(text: str, rng: random.Random) -> str | None:
    spans = [m for m in _NUMBER_SPAN_RE.finditer(text) if numbers(m.group(0))]
    if not spans:
        return None
    target = rng.choice(spans)
    return text[: target.start()] + _scaled(target, rng.choice(FACTORS)) + text[target.end() :]


def swap_arms(text: str, arms: list[str]) -> str | None:
    present = [arm for arm in arms if re.search(rf"(?<!\w){re.escape(arm)}(?!\w)", text)]
    if not present or len(arms) < 2:
        return None
    pairs = {present[0]: next(a for a in arms if a != present[0])}
    if len(present) > 1:
        pairs = {present[0]: present[1], present[1]: present[0]}
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(a)}(?!\w)" for a in pairs))
    return pattern.sub(lambda m: pairs[m.group(0)], text)


def flip_direction(text: str) -> str | None:
    normal = normalize(text)
    for left, right in _DIRECTION_SWAPS:
        for a, b in ((left, right), (right, left)):
            pattern = re.compile(rf"(?<!\w){re.escape(a)}(?!\w)" if a[0].isalpha() else re.escape(a), re.IGNORECASE)
            if pattern.search(normal):
                return pattern.sub(b, normal, count=1)
    return None


def itt_to_pp(text: str) -> str | None:
    return _ITT_RE.sub("per-protocol", text, count=1) if _ITT_RE.search(text) else None


def corrupt(fact: dict, kind: str, rng: random.Random, arms: list[str]) -> dict | None:
    """A corrupted copy of a present fact, or None if `kind` does not apply to it."""
    if fact["status"] != "present":
        return None
    if kind == "to_absent":
        return dict(fact, text="Not reported in protocol.", status="absent", item=fact["item"] or fact["field"], planted=kind)
    text = {
        "scale": lambda: scale_numbers(fact["text"], rng),
        "arm_swap": lambda: swap_arms(fact["text"], arms),
        "direction_flip": lambda: flip_direction(fact["text"]),
        "itt_to_pp": lambda: itt_to_pp(fact["text"]),
    }[kind]()
    if not text or text == fact["text"]:
        return None
    return dict(fact, text=text, planted=kind)


def make(documents: list[dict], n: int, seed: int) -> tuple[list[dict], list[dict]]:
    """documents: [{doc_dir, facts, arms}]. Returns (blind items, key)."""
    rng = random.Random(seed)
    pool = []
    for doc in documents:
        for fact in doc["facts"]:
            if fact["status"] == "present" and fact["cited_pages"]:
                pool.append((doc, fact))
    rng.shuffle(pool)
    # Rarer corruptions first, so the set is not all number scaling.
    chosen: list[tuple[dict, dict, dict]] = []
    used: set[tuple[str, str]] = set()
    per_kind = max(1, n // len(KINDS))
    for kind in ("arm_swap", "direction_flip", "itt_to_pp", "to_absent", "scale"):
        quota = per_kind if kind != "scale" else n - len(chosen)
        for doc, fact in pool:
            if quota <= 0 or len(chosen) >= n:
                break
            key = (fact["doc_id"], fact["fact_id"])
            if key in used:
                continue
            bad = corrupt(fact, kind, rng, doc["arms"])
            if bad is not None:
                chosen.append((doc, fact, bad))
                used.add(key)
                quota -= 1
    items, key = [], []
    for doc, fact, bad in chosen:
        for version, planted in ((fact, None), (bad, bad["planted"])):
            items.append({"doc_dir": doc["doc_dir"], "fact": {k: v for k, v in version.items() if k != "planted"}})
            key.append({"planted": planted, "orig_fact_id": fact["fact_id"], "doc_id": fact["doc_id"]})
    order = list(range(len(items)))
    rng.shuffle(order)
    blind, blind_key = [], []
    for number, index in enumerate(order):
        item = items[index]
        item["fact"] = dict(item["fact"], fact_id=f"P{number:04d}")
        blind.append(item)
        blind_key.append(dict(key[index], blind_id=f"P{number:04d}"))
    return blind, blind_key


def score(key: list[dict], verdicts: dict[str, dict]) -> dict:
    from qc.metrics import clopper_pearson

    def flagged(verdict: dict) -> bool:
        return verdict["verdict"] not in ("supported", "consistent")

    out: dict = {"by_kind": {}}
    corrupted = [k for k in key if k["planted"] and k["blind_id"] in verdicts]
    originals = [k for k in key if not k["planted"] and k["blind_id"] in verdicts]
    hits = sum(flagged(verdicts[k["blind_id"]]) for k in corrupted)
    alarms = sum(flagged(verdicts[k["blind_id"]]) for k in originals)
    out["detection"] = {"n": len(corrupted), "rate": hits / len(corrupted) if corrupted else None,
                        "ci": clopper_pearson(hits, len(corrupted))}
    out["false_alarm"] = {"n": len(originals), "rate": alarms / len(originals) if originals else None,
                          "ci": clopper_pearson(alarms, len(originals))}
    for kind in KINDS:
        group = [k for k in corrupted if k["planted"] == kind]
        if group:
            found = sum(flagged(verdicts[k["blind_id"]]) for k in group)
            out["by_kind"][kind] = {"n": len(group), "detected": found, "rate": found / len(group)}
    out["gate"] = {"detection>=0.90": (out["detection"]["rate"] or 0) >= 0.90,
                   "false_alarm<=0.05": out["false_alarm"]["rate"] is not None and out["false_alarm"]["rate"] <= 0.05}
    return out


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    from qc import QC_ROOT

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    make_p = sub.add_parser("make")
    make_p.add_argument("--out", type=Path, default=QC_ROOT / "out")
    make_p.add_argument("--docs", nargs="+", required=True, help="processed doc dirs under --out (doi_dir names)")
    make_p.add_argument("--n", type=int, default=100)
    make_p.add_argument("--seed", type=int, default=7)
    judge_p = sub.add_parser("judge")
    judge_p.add_argument("--out", type=Path, default=QC_ROOT / "out")
    judge_p.add_argument("--pairs", type=Path, required=True, help="pairs.csv the documents came from")
    judge_p.add_argument("--backend", choices=("openai", "stub"), default="openai")
    judge_p.add_argument("--dense", choices=("openai", "tfidf", "none"), default=None)
    score_p = sub.add_parser("score")
    score_p.add_argument("--out", type=Path, default=QC_ROOT / "out")
    args = parser.parse_args()
    planted_dir = args.out / "_planted"

    if args.command == "make":
        from qc.triggers import arm_labels

        documents = []
        for doc_dir in args.docs:
            for facts_path in sorted((args.out / doc_dir).glob("*.facts.jsonl")):
                stem = facts_path.name[: -len(".facts.jsonl")]
                claims = _read_jsonl(facts_path.with_name(f"{stem}.claims.jsonl"))
                documents.append({"doc_dir": doc_dir, "facts": _read_jsonl(facts_path), "arms": arm_labels(claims)})
        blind, key = make(documents, args.n, args.seed)
        _write_jsonl(planted_dir / "planted.jsonl", blind)
        _write_jsonl(planted_dir / "key.jsonl", key)
        print(f"planted {sum(1 for k in key if k['planted'])} corrupted + originals = {len(blind)} items -> {planted_dir}")
        return 0

    if args.command == "judge":
        from qc.run_qc import judge_planted

        verdicts = judge_planted(_read_jsonl(planted_dir / "planted.jsonl"), args)
        _write_jsonl(planted_dir / "verdicts.jsonl", [dict(v, fact_id=k) for k, v in verdicts.items()])
        print(f"judged {len(verdicts)} planted items -> {planted_dir / 'verdicts.jsonl'}")
        return 0

    verdicts = {row["fact_id"]: row for row in _read_jsonl(planted_dir / "verdicts.jsonl")}
    result = score(_read_jsonl(planted_dir / "key.jsonl"), verdicts)
    (planted_dir / "score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
