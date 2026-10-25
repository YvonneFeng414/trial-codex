"""Claims -> atomic facts (D4). One model call per SAP, cached by the SAP's hash.

Every fact must be a span or paraphrase of its claim with no new content; a check confirms
that each fact's numbers and named entities occur in the parent claim. Claims that fail
are re-asked once, then fall back to the deterministic rule atomizer, which is also the
atomizer for stub and no-token (P1) runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import qc  # noqa: F401
from lint_sap import ABSENCE_RE  # type: ignore[import-not-found]
from qc.text import STOPWORDS, number_values, sha

# Absence wordings ABSENCE_RE does not cover: "no other multiplicity adjustment is reported".
EXTRA_ABSENCE_RE = re.compile(
    r"\bno\s+(?P<item>[^.;]{0,80}?)\s+(?:is|are|was|were)?\s*(?:reported|specified|stated|described)\b"
    r"|\bno\s+(?P<item2>[^.;]{0,60}?)\s+(?:reported|specified)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)\s+(?=[A-Z(])|(?<=;)\s+")
_ENTITY_RE = re.compile(r"\b[A-Z][\w\-’']*")
_COPULA_RE = re.compile(r"\s+(?:is|are|was|were)$", re.IGNORECASE)


def absence_match(text: str) -> re.Match[str] | None:
    return ABSENCE_RE.search(text) or EXTRA_ABSENCE_RE.search(text)


def is_absent(text: str) -> bool:
    return absence_match(text) is not None


# --- rule atomizer -----------------------------------------------------------------------


def _strip_label(line: str, field: str) -> tuple[str, str | None]:
    """Drop a leading `Field:` and return a nested item's own label (`TAH: ...`)."""
    if field and line.lower().startswith(field.lower()):
        line = line[len(field) :].lstrip(" :")
    label = None
    nested = re.match(r"^(?P<label>[^:]{1,40}):\s+(?P<rest>.+)$", line)
    if nested and not re.search(r"\d", nested.group("label")):
        label = nested.group("label").strip()
    return line, label


def _absent_item(sentence: str, field: str) -> str:
    match = absence_match(sentence)
    if match is None:
        return field
    item = match.groupdict().get("item") or match.groupdict().get("item2")
    if item:
        return item.strip()
    before = _COPULA_RE.sub("", sentence[: match.start()].strip(" ,;:")).strip()
    after = sentence[match.end() :].strip(" .")
    after = re.sub(r"^in protocol\b\s*", "", after).strip(" .")
    if before:
        return before
    return f"{field} ({after})" if after else field


def rule_facts(claim: dict) -> list[dict]:
    facts: list[dict] = []
    for index, line in enumerate(claim["lines"]):
        line, label = _strip_label(line, claim["field"] if index == 0 else "")
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if not sentence or len(re.findall(r"\w+", sentence)) < 1:
                continue
            absent = is_absent(sentence)
            item = label or claim["field"]
            facts.append(
                {
                    "text": sentence,
                    "item": _absent_item(sentence, item) if absent else item,
                    "value": None,
                    "status": "absent" if absent else "present",
                }
            )
    if not facts:
        facts.append({"text": claim["text"], "item": claim["field"], "value": None,
                      "status": "absent" if claim["absence"] else "present"})
    return facts


# --- model atomizer ----------------------------------------------------------------------


class AtomFact(BaseModel):
    text: str = Field(description="One atomic statement: a span or close paraphrase of the claim")
    item: str = Field(description="What the statement is about, e.g. 'power', 'stratification factor'")
    value: str | None = Field(description="The value stated for the item, verbatim, or null")
    status: Literal["present", "absent"] = Field(
        description="'absent' if the statement says the item is not reported/specified/applicable"
    )


class ClaimFacts(BaseModel):
    claim_id: int
    facts: list[AtomFact]


class Atomization(BaseModel):
    claims: list[ClaimFacts]


INSTRUCTIONS = """You split claims from a Statistical Analysis Plan into atomic facts.

Rules:
- Each fact states exactly one checkable thing: one value, one criterion, one rule, one
  time point, or one statement that something is not reported.
- Each fact is a span or a close paraphrase of its claim. Add nothing: no new numbers,
  names, arms, qualifiers or inferences. Keep the claim's own numbers and terms verbatim.
- Keep qualifiers that change meaning (one-sided, >=, per-protocol, Stage 2, arm names)
  inside the fact they qualify.
- A list of criteria, arms, objectives or time points becomes one fact per element.
- A statement that something is "not reported", "not specified", "not applicable", or
  "no other ... is reported" is one fact with status "absent"; its item names what is
  absent. Everything else has status "present".
- Return every claim_id you were given, in order, with at least one fact each.
"""


def _reference(claim: dict) -> str:
    return " ".join(filter(None, [claim["text"], claim["field"], claim.get("outcome_block") or "", claim["section"]]))


def check_fact(fact_text: str, claim: dict) -> list[str]:
    """Numbers and named entities in the fact that its parent claim does not contain."""
    reference = _reference(claim)
    lowered = reference.lower()
    problems = [str(n) for n in set(number_values(fact_text)) - set(number_values(reference))]
    for word in _ENTITY_RE.findall(fact_text):
        token = word.strip("-’'").lower()
        if len(token) > 1 and token not in STOPWORDS and token not in lowered:
            problems.append(word)
    return problems


def _payload(claims: list[dict]) -> str:
    rows = [
        {"claim_id": c["claim_id"], "section": c["section"], "field": c["field"], "text": c["text"]}
        for c in claims
    ]
    return json.dumps(rows, ensure_ascii=False)


def _model_atomize(claims: list[dict], backend, doc_id: str) -> dict[int, list[dict]]:
    by_id = {c["claim_id"]: c for c in claims}
    result = backend.parse(
        role="atomizer", instructions=INSTRUCTIONS, payload=_payload(claims), schema=Atomization,
        doc_id=doc_id, stage="atomize", n_items=len(claims),
    )
    out: dict[int, list[dict]] = {}
    for entry in result.claims:
        if entry.claim_id in by_id and entry.facts:
            out[entry.claim_id] = [fact.model_dump() for fact in entry.facts]
    return out


def atomize(claims: list[dict], backend, doc_id: str, cache_dir: Path, md_sha: str) -> list[dict]:
    """Return fact records in claim order; `backend=None` uses the rule atomizer."""
    model = (backend.config["models"].get("atomizer") or backend.config["models"]["judge"]) if backend else "rule"
    cache = cache_dir / f"atoms.{sha([md_sha, model, sha(INSTRUCTIONS)])}.json"
    if cache.is_file():
        produced = {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    else:
        produced = {}
        if backend is not None:
            produced = _model_atomize(claims, backend, doc_id)
            failing = [
                c for c in claims
                if c["claim_id"] not in produced
                or any(check_fact(f["text"], c) for f in produced[c["claim_id"]])
            ]
            if failing:
                retry = _model_atomize(failing, backend, doc_id)
                for claim in failing:
                    facts = retry.get(claim["claim_id"])
                    if facts and not any(check_fact(f["text"], claim) for f in facts):
                        produced[claim["claim_id"]] = facts
                    else:
                        produced[claim["claim_id"]] = [dict(f, source="rule_fallback") for f in rule_facts(claim)]
        for claim in claims:
            produced.setdefault(claim["claim_id"], [dict(f, source="rule") for f in rule_facts(claim)])
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(produced, ensure_ascii=False, indent=1), encoding="utf-8")

    facts: list[dict] = []
    for claim in claims:
        for index, atom in enumerate(produced[claim["claim_id"]]):
            facts.append(
                {
                    "doc_id": claim["doc_id"],
                    "claim_id": claim["claim_id"],
                    "fact_id": f"{claim['claim_id']}.{index}",
                    "section": claim["section"],
                    "field": claim["field"],
                    "outcome_block": claim["outcome_block"],
                    "text": atom["text"],
                    "item": atom.get("item") or claim["field"],
                    "value": atom.get("value"),
                    "status": atom["status"],
                    "cited_pages": claim["cited_pages"],
                    "sweep": claim["sweep"],
                    "md_line": claim["md_line"],
                    "source": atom.get("source", "model" if backend else "rule"),
                    "n_claim_facts": len(produced[claim["claim_id"]]),
                }
            )
    return facts
