"""Judge facts against evidence passages, verify every quote, apply the fixed checks.

The judge sees SAP facts and protocol passages only, never the generator's evidence or
review files (D7). A supported or contradicted verdict must carry a verbatim quote, which
is checked by string match against the pages shown (D9): a failed quote is re-asked once,
then becomes `not_found` and goes to manual review.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from qc.packets import PassageIndex
from qc.text import numbers, page_variants, quote_matches, sha, tokenize
from qc.triggers import polarity_conflicts


class FactVerdict(BaseModel):
    fact_id: str
    verdict: Literal["supported", "contradicted", "not_found"]
    minor: bool = Field(description="contradicted only: the discrepancy is minor (imprecise or incomplete, not wrong)")
    quote: str = Field(description="Verbatim text from ONE passage; '...' joins pieces of it. Empty for not_found")
    quote_page: int | None = Field(description="Page of the quoted passage")
    supporting_pages: list[int] = Field(description="Which of the fact's cited pages support it")
    rationale: str = Field(description="At most 30 words")


class AbsenceVerdict(BaseModel):
    fact_id: str
    verdict: Literal["consistent", "contradicted", "unverifiable"]
    minor: bool = Field(description="contradicted only: the protocol mentions the item only in passing")
    quote: str = Field(description="Verbatim text that specifies the item. Empty for consistent")
    quote_page: int | None
    rationale: str = Field(description="At most 30 words")


class PresentBatch(BaseModel):
    verdicts: list[FactVerdict]


class AbsenceBatch(BaseModel):
    verdicts: list[AbsenceVerdict]


PRESENT_INSTRUCTIONS = """You check statements (facts) from a Statistical Analysis Plan (SAP)
against passages from the trial protocol. Judge each fact only from the passages given.

Verdicts:
- supported: a passage states the fact. Paraphrase is fine, but every number, unit, arm,
  population, direction (>=, <=, upper, lower), sidedness and hypothesis type must match.
- contradicted: a passage states something incompatible with the fact. Set minor=true
  only if the fact is imprecise or incomplete rather than wrong.
- not_found: the passages neither state nor contradict it.

If the protocol is inconsistent with itself (for example equivalence in one section and
non-inferiority in another) and the fact matches one version, it is supported; say so in
the rationale.

For supported and contradicted, `quote` must be copied verbatim from ONE passage, and
must contain every number the fact states; use "..." to join pieces of the same passage.
`quote_page` is that passage's page. `supporting_pages` lists which of the fact's cited
pages support it (may be empty). Keep `rationale` under 30 words. Return one verdict
for every fact_id, in order.
"""

ABSENCE_INSTRUCTIONS = """You check SAP statements that say an item is NOT reported in the trial
protocol. For each fact, answer: does any passage specify the item? Quote it.

Verdicts:
- contradicted: a passage specifies the item. Copy the specifying text verbatim from ONE
  passage into `quote` ("..." may join pieces of it) and give its page. Set minor=true if
  the item is only mentioned in passing or partially.
- consistent: no passage specifies the item.
- unverifiable: the passages are too garbled or incomplete to tell.

A passage that specifies a different item, or the item for a different outcome or
population than the fact names, does not contradict it. Keep `rationale` under 30 words.
Return one verdict for every fact_id, in order.
"""


def _passage_rows(index: PassageIndex, ids: list[int]) -> list[dict]:
    return [{"page": index.passages[i]["page"], "passage": index.passages[i]["id"], "text": index.passages[i]["text"]} for i in ids]


def _payload(facts: list[dict], index: PassageIndex, passage_ids: list[int], note: str = "") -> str:
    rows = []
    for fact in facts:
        row = {"fact_id": fact["fact_id"], "section": fact["section"], "field": fact["field"], "text": fact["text"]}
        if fact.get("outcome_block"):
            row["outcome"] = fact["outcome_block"]
        if fact["status"] == "absent":
            row["item"] = fact["item"]
        else:
            row["cited_pages"] = fact["cited_pages"]
        rows.append(row)
    body = {"facts": rows, "passages": _passage_rows(index, passage_ids)}
    if note:
        body["note"] = note
    return json.dumps(body, ensure_ascii=False)


def stub_verdicts(facts: list[dict], index: PassageIndex, passage_ids: list[int]) -> list[dict]:
    """Deterministic stand-in for the model: quotes the best-overlapping passage verbatim."""
    out = []
    for fact in facts:
        if fact["status"] == "absent":
            out.append({"fact_id": fact["fact_id"], "verdict": "consistent", "minor": False, "quote": "",
                        "quote_page": None, "rationale": "stub"})
            continue
        wanted = set(tokenize(fact["text"]))
        best, overlap = None, 0.0
        for i in passage_ids:
            have = set(tokenize(index.passages[i]["text"]))
            score = len(wanted & have) / max(len(wanted), 1)
            if score > overlap:
                best, overlap = i, score
        if best is None or overlap < 0.5:
            out.append({"fact_id": fact["fact_id"], "verdict": "not_found", "minor": False, "quote": "",
                        "quote_page": None, "supporting_pages": [], "rationale": "stub"})
            continue
        passage = index.passages[best]
        out.append({"fact_id": fact["fact_id"], "verdict": "supported", "minor": False,
                    "quote": " ".join(passage["text"].split()[:15]), "quote_page": passage["page"],
                    "supporting_pages": [passage["page"]] if passage["page"] in fact["cited_pages"] else [],
                    "rationale": f"stub: token overlap {overlap:.2f}"})
    return out


class Judge:
    def __init__(self, backend, pages: list[dict], index: PassageIndex, config: dict, arms: list[str], doc_id: str,
                 front=None):
        self.backend = backend
        self.front = front  # JEV front end (qc.jev), or None for the LLM alone
        self.front_errors: list[str] = []
        self.index = index
        self.config = config
        self.arms = arms
        self.doc_id = doc_id
        self.min_chars = config["judge"]["min_quote_chars"]
        self.variants = {page["page"]: page_variants(page["text"], page["clean"]) for page in pages}
        self.model = "stub" if backend is None else backend.config["models"]["judge"]

    def _ask(self, facts: list[dict], passage_ids: list[int], note: str = "") -> list[dict]:
        if self.backend is None:
            return stub_verdicts(facts, self.index, passage_ids)
        absent = facts[0]["status"] == "absent"
        result = self.backend.parse(
            role="judge",
            instructions=ABSENCE_INSTRUCTIONS if absent else PRESENT_INSTRUCTIONS,
            payload=_payload(facts, self.index, passage_ids, note),
            schema=AbsenceBatch if absent else PresentBatch,
            doc_id=self.doc_id, stage="judge_absence" if absent else "judge", n_items=len(facts),
        )
        return [verdict.model_dump() for verdict in result.verdicts]

    def quote_ok(self, verdict: dict, shown_pages: set[int]) -> bool:
        if verdict["verdict"] not in ("supported", "contradicted"):
            return True
        page = verdict.get("quote_page")
        return page in shown_pages and quote_matches(verdict.get("quote", ""), self.variants.get(page, []), self.min_chars)

    def fixed_checks(self, fact: dict, verdict: dict) -> list[str]:
        """Numbers and polarity re-checked against the quote of a supported present fact."""
        if fact["status"] != "present" or verdict["verdict"] != "supported":
            return []
        overrides = []
        if numbers(fact["text"]) - numbers(verdict.get("quote", "")):
            overrides.append("NUM_MISS")
        if polarity_conflicts(fact["text"], verdict.get("quote", ""), self.arms):
            overrides.append("POLARITY")
        return overrides

    def run_call(self, facts: list[dict], passage_ids: list[int], codes: dict[str, list[str]] | None = None) -> dict[str, dict]:
        """JEV first when a front end is set; the LLM judges whatever JEV does not settle."""
        if self.front is None:
            return self._llm_call(facts, passage_ids)
        from qc.jev import accepts

        codes = codes or {}
        settings = self.config["jev"]
        try:
            decisions = self.front.decide(facts, self.index, passage_ids)
        except Exception as exc:  # e.g. a packet over JEV's context: the LLM judges these facts
            self.front_errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
            decisions = {}
        shown = sorted({self.index.passages[i]["page"] for i in passage_ids})
        out: dict[str, dict] = {}
        escalate = []
        for fact in facts:
            decision = decisions.get(fact["fact_id"])
            if accepts(fact, decision, codes.get(fact["fact_id"], []), settings):
                out[fact["fact_id"]] = {
                    "verdict": decision["choice"], "raw_verdict": decision["choice"], "minor": False,
                    "quote": "", "quote_page": None, "quote_verified": False, "supporting_pages": [],
                    "rationale": f"JEV {decision['choice']} at confidence {decision['confidence']:.3f}",
                    "override": None, "needs_review": False, "retried": False,
                    "judge_model": decision["model"], "judge_stage": "jev", "jev": decision,
                    "prompt_sha": None, "evidence_pages": shown,
                }
            else:
                escalate.append(fact)
        if escalate:
            for fact_id, verdict in self._llm_call(escalate, passage_ids).items():
                out[fact_id] = dict(verdict, jev=decisions.get(fact_id))
        return out

    def _llm_call(self, facts: list[dict], passage_ids: list[int]) -> dict[str, dict]:
        shown = {self.index.passages[i]["page"] for i in passage_ids}
        prompt = ABSENCE_INSTRUCTIONS if facts[0]["status"] == "absent" else PRESENT_INSTRUCTIONS
        answers = {v["fact_id"]: v for v in self._ask(facts, passage_ids)}
        failed = [f for f in facts if f["fact_id"] not in answers or not self.quote_ok(answers[f["fact_id"]], shown)]
        retried: set[str] = set()
        if failed:
            note = ("Your previous answer for these facts gave a quote that does not occur verbatim on the "
                    "quoted page, or omitted the fact. Copy the quote character for character from one passage.")
            for verdict in self._ask(failed, passage_ids, note):
                if verdict["fact_id"] in {f["fact_id"] for f in failed}:
                    answers[verdict["fact_id"]] = verdict
                    retried.add(verdict["fact_id"])

        out: dict[str, dict] = {}
        for fact in facts:
            verdict = dict(answers.get(fact["fact_id"]) or {"verdict": "not_found", "quote": "", "quote_page": None, "minor": False, "rationale": "no answer"})
            raw = verdict["verdict"]
            verified = self.quote_ok(verdict, shown)
            needs_review = False
            if not verified:
                verdict.update(verdict="not_found" if fact["status"] == "present" else "unverifiable")
                needs_review = True
            overrides = self.fixed_checks(fact, verdict) if verified else []
            if overrides:
                needs_review = True
            out[fact["fact_id"]] = {
                "verdict": verdict["verdict"],
                "raw_verdict": raw,
                "minor": bool(verdict.get("minor")) and verdict["verdict"] == "contradicted",
                "quote": verdict.get("quote", ""),
                "quote_page": verdict.get("quote_page"),
                "quote_verified": verified and verdict["verdict"] in ("supported", "contradicted"),
                "supporting_pages": [p for p in verdict.get("supporting_pages", []) if p in fact["cited_pages"]],
                "rationale": verdict.get("rationale", ""),
                "override": overrides or None,
                "needs_review": needs_review,
                "retried": fact["fact_id"] in retried,
                "judge_model": self.model,
                "judge_stage": "llm",
                "prompt_sha": sha(prompt),
                "evidence_pages": sorted(shown),
            }
        return out
