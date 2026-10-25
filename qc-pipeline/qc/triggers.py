"""Risk triggers: any one puts a fact in T, the set judged in full (D2).

The trigger set is frozen at the end of P2 (TRIGGERS_VERSION). Tuning it on the same
documents it is reported on would flatter it; R ∩ U measures what it misses.
"""

from __future__ import annotations

import re

import numpy as np

import qc  # noqa: F401
from lint_sap import FIELD_TOPICS  # type: ignore[import-not-found]
from qc.absence import matches_any
from qc.claims import section_number
from qc.packets import PassageIndex, fact_query
from qc.text import normalize, numbers

TRIGGERS_VERSION = "v1"
CODES = ("NUM_MISS", "POLARITY", "COVERAGE_MISS", "SWEEP_ONLY", "ABSENCE_HITS",
         "SELF_CONTRA", "LOW_OVERLAP", "COMPOUND", "CRITICAL")

# Each class lists mutually exclusive values; a fact disagrees with a passage when both
# name values of a class and share none.
POLARITY_CLASSES: dict[str, dict[str, re.Pattern[str]]] = {
    "direction": {
        "ge": re.compile(r"≥|>=|(?<![<-])>(?!=)|\bat least\b|\bno (?:less|fewer) than\b|\bor (?:more|greater|above)\b|\b(?:greater|more) than\b|\bexceed", re.I),
        "le": re.compile(r"≤|<=|(?<!-)<(?!=)|\bat most\b|\bno (?:more|greater) than\b|\bor (?:less|fewer|below)\b|\b(?:less|fewer) than\b|\bbelow\b", re.I),
    },
    "bound": {
        "upper": re.compile(r"\bupper\b", re.I),
        "lower": re.compile(r"\blower\b", re.I),
    },
    "sided": {
        "one": re.compile(r"\bone[- ]?(?:sided|tailed)\b|\bunilateral\b", re.I),
        "two": re.compile(r"\btwo[- ]?(?:sided|tailed)\b|\bbilateral\b", re.I),
    },
    "hypothesis": {
        "superiority": re.compile(r"(?<!non-)(?<!non)\bsuperiority\b", re.I),
        "noninferiority": re.compile(r"\bnon-?\s?inferior", re.I),
        "equivalence": re.compile(r"\bequivalen", re.I),
    },
}
_ARM_LABEL_RE = re.compile(r"^(?:[A-Z][A-Z0-9+/-]{1,11}|(?:Group|Arm|Cohort)\s+[A-Z0-9]{1,3})$")


def arm_labels(claims: list[dict]) -> list[str]:
    """Arm labels the SAP itself enumerates under Intervention Summary ("TAH: ...")."""
    labels: list[str] = []
    for claim in claims:
        if claim["field"] != "Intervention Summary":
            continue
        for line in claim["lines"][1:]:
            match = re.match(r"^(?P<label>[^:]{1,20}):\s", line)
            if match and _ARM_LABEL_RE.match(match.group("label").strip()):
                labels.append(match.group("label").strip())
    return list(dict.fromkeys(labels))


def polarity(text: str, arms: list[str]) -> dict[str, set[str]]:
    text = normalize(text)
    found = {
        name: {value for value, pattern in values.items() if pattern.search(text)}
        for name, values in POLARITY_CLASSES.items()
    }
    found["arm"] = {arm for arm in arms if re.search(rf"(?<![\w]){re.escape(arm)}(?![\w])", text)}
    return {name: values for name, values in found.items() if values}


def polarity_conflicts(fact_text: str, evidence_text: str, arms: list[str]) -> list[str]:
    mine, theirs = polarity(fact_text, arms), polarity(evidence_text, arms)
    return [name for name, values in mine.items() if name in theirs and not values & theirs[name]]


def missing_numbers(fact_text: str, page_texts: list[str]) -> list[float]:
    wanted = numbers(fact_text)
    if not wanted:
        return []
    found: set[float] = set()
    for text in page_texts:
        found |= numbers(text)
    return sorted(wanted - found)


def self_contradiction(fact: dict, facts: list[dict], claims_by_id: dict[int, dict], lexicon: dict) -> str | None:
    """An absence fact the SAP answers itself: in its own outcome block (by the field's
    lexicon terms) or, for lint_sap FIELD_TOPICS fields, anywhere in the SAP. A claim that
    names part of its own topic is scoping, not a blanket gap, as in lint_sap."""
    if fact["status"] != "absent":
        return None
    own = claims_by_id[fact["claim_id"]]
    # The claim's own label ("Transformations / Derived Variables:") is not content; left
    # in, it matches the field's own terms and every such claim would exempt itself.
    own_text = re.sub(rf"^{re.escape(fact['field'])}:?\s*", "", own["text"])
    others = [f for f in facts if f["status"] == "present" and f["claim_id"] != fact["claim_id"]]
    if fact.get("outcome_block"):
        terms = lexicon.get("fields", {}).get(fact["field"], [])
        if terms and not matches_any(own_text.replace(fact["text"], ""), terms):
            for other in others:
                if other.get("outcome_block") == fact["outcome_block"] and other["section"] == fact["section"]:
                    hits = matches_any(other["text"], terms)
                    if hits:
                        return f"outcome block: fact {other['fact_id']} states '{hits[0]}'"
    for label, patterns in FIELD_TOPICS:
        if fact["field"] != label:
            continue
        topic = re.compile("|".join(patterns), re.IGNORECASE)
        if topic.search(own_text):
            return None
        for other in others:
            hit = topic.search(other["text"])
            if hit:
                return f"field topic: fact {other['fact_id']} ({other['field']}) states '{hit.group(0)}'"
    return None


def evaluate(facts, retrievals, claims, pages, passages: PassageIndex, absence_found, lexicon, config) -> dict[str, dict]:
    """Return {fact_id: {"codes": [...], "detail": {...}}} for every fact."""
    settings = config["triggers"]
    k = config["retrieval"]["k"]
    page_text = {page["page"]: page["clean"] for page in pages}
    claims_by_id = {claim["claim_id"]: claim for claim in claims}
    arms = arm_labels(claims)
    by_id = {r["fact_id"]: r for r in retrievals}

    best_scores: dict[str, float] = {}
    best_passage: dict[str, int] = {}
    for fact in facts:
        if fact["cited_pages"]:
            scores = passages.scores(fact_query(fact))
            best = passages.best("", fact["cited_pages"], 1, scores)
            if best:
                best_passage[fact["fact_id"]] = best[0]
                best_scores[fact["fact_id"]] = float(scores[best[0]])
    low_cut = float(np.quantile(list(best_scores.values()), settings["low_overlap_quantile"])) if best_scores else None

    out: dict[str, dict] = {}
    for fact in facts:
        codes: list[str] = []
        detail: dict = {}
        cited = fact["cited_pages"]
        present = fact["status"] == "present"
        if present and cited:
            missing = missing_numbers(fact["text"], [page_text.get(p, "") for p in cited])
            if missing:
                codes.append("NUM_MISS")
                detail["missing_numbers"] = missing
            if fact["fact_id"] in best_passage:
                passage = passages.passages[best_passage[fact["fact_id"]]]
                conflicts = polarity_conflicts(fact["text"], passage["text"], arms)
                if conflicts:
                    codes.append("POLARITY")
                    detail["polarity"] = conflicts
                detail["best_passage"] = passage["id"]
        if cited:
            rank = by_id[fact["fact_id"]]["cited_rank"]
            if rank is None or rank > k:
                codes.append("COVERAGE_MISS")
        else:
            codes.append("SWEEP_ONLY")
        if not present and absence_found.get(fact["fact_id"], {}).get("pages"):
            codes.append("ABSENCE_HITS")
        reason = self_contradiction(fact, facts, claims_by_id, lexicon)
        if reason:
            codes.append("SELF_CONTRA")
            detail["self_contra"] = reason
        if low_cut is not None and fact["fact_id"] in best_scores and best_scores[fact["fact_id"]] <= low_cut:
            codes.append("LOW_OVERLAP")
            detail["best_bm25"] = round(best_scores[fact["fact_id"]], 3)
        if fact["n_claim_facts"] > settings["compound_facts"]:
            codes.append("COMPOUND")
        if section_number(fact["section"]) in settings["critical_sections"]:
            codes.append("CRITICAL")
        out[fact["fact_id"]] = {"codes": codes, "detail": detail}
    return out
