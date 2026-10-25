"""JEV (TypeSafe AI's decision model) as the judge's front end.

JEV answers each fact as a typed Choice over the evidence passages, with probabilities and
no text. In `cascade` mode a confident `supported` / `consistent` is accepted as the
verdict, and everything else escalates to the LLM judge, which must quote. So every error
the pipeline reports still carries a verified quote; only JEV-accepted verdicts have
none, a deliberate relaxation of D9 that P3 measures (planted errors, gold labels).
In `shadow` mode JEV answers every fact but the LLM decides them all, which gives the
paired data `run_qc.py calibrate` uses to set the acceptance threshold.

Following the JEV-as-a-Judge paper, each call runs with the passages in both orders
and the probabilities are averaged; confidence is the largest averaged probability.
"""

from __future__ import annotations

import os

from qc.config import ConfigError
from qc.packets import PassageIndex
from qc.text import tokenize

ACCEPT = {"present": "supported", "absent": "consistent"}
PRESENT_CRITERIA = {
    "supported": "A passage states the fact. Paraphrase is fine, but every number, unit, arm, population, "
                 "direction (>=, <=, upper, lower), sidedness and hypothesis type matches the passage.",
    "contradicted": "A passage states something incompatible with the fact.",
    "not_found": "The passages neither state nor contradict the fact, or are too incomplete to tell.",
}
ABSENCE_CRITERIA = {
    "consistent": "No passage specifies the item for the outcome or population the statement names.",
    "contradicted": "A passage specifies the item.",
    "unverifiable": "The passages are too garbled or incomplete to tell.",
}
# Facts the deterministic checks already doubt are never accepted on JEV's word alone:
# a number missing from the cited pages, a polarity clash, or an absence claim the SAP
# itself answers elsewhere (SELF_CONTRA, pilot claim C96).
NO_ACCEPT_TRIGGERS = frozenset({"NUM_MISS", "POLARITY", "SELF_CONTRA"})


def question_text(fact: dict) -> str:
    where = f"SAP section {fact['section']}, field '{fact['field']}'"
    if fact.get("outcome_block"):
        where += f", outcome '{fact['outcome_block']}'"
    if fact["status"] == "absent":
        return (f"A Statistical Analysis Plan ({where}) says this item is NOT reported in the trial protocol: "
                f"\"{fact['item']}\" (statement: \"{fact['text']}\"). Using only the protocol passages in the "
                "state, does any passage specify the item?")
    return (f"A Statistical Analysis Plan ({where}) states: \"{fact['text']}\". It cites protocol pages "
            f"{fact['cited_pages'] or 'none'}. Using only the protocol passages in the state, is the statement "
            "supported? If the protocol contradicts itself and the statement matches one version, it is supported.")


def state(index: PassageIndex, passage_ids: list[int]) -> dict:
    return {"protocol_passages": [
        {"page": index.passages[i]["page"], "passage": index.passages[i]["id"], "text": index.passages[i]["text"]}
        for i in passage_ids
    ]}


def average(runs: list[dict[str, float]]) -> dict[str, float]:
    labels = set().union(*runs)
    return {label: sum(run.get(label, 0.0) for run in runs) / len(runs) for label in labels}


def decision(probabilities: dict[str, float], model: str) -> dict:
    choice = max(sorted(probabilities), key=lambda label: probabilities[label])
    return {"choice": choice, "confidence": probabilities[choice],
            "probabilities": {k: round(v, 4) for k, v in sorted(probabilities.items())}, "model": model}


def require_typesafe_key() -> str:
    from qc.llm import ENV_FILE, load_env

    load_env()
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise ConfigError(f"TYPESAFE_API_KEY is not set: add it to {ENV_FILE}, or run with --jev off")
    return key


class JevFrontEnd:
    """Calls JEV through the official TypeSafe Python SDK."""

    def __init__(self, config: dict, cost, doc_id: str):
        from typesafe_sdk import TypeSafeClient

        settings = config["jev"]
        key = require_typesafe_key()
        self.settings = settings
        self.model = settings["model"]
        self.cost = cost
        self.doc_id = doc_id
        self.client = TypeSafeClient(api_key=key, model=self.model, timeout=settings.get("timeout", 30))

    def _ask(self, payload: dict, questions: dict) -> dict[str, dict[str, float]]:
        response = self.client.system_one(state=payload, questions=questions, model=self.model)
        usage = response.usage
        self.cost.add(doc_id=self.doc_id, stage="jev", model=response.model, prompt_sha="",
                      input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                      n_items=len(questions))
        return {name: dict(answer.probabilities) for name, answer in response.choices.items()}

    def decide(self, facts: list[dict], index: PassageIndex, passage_ids: list[int]) -> dict[str, dict]:
        from typesafe_sdk import Choice

        names = {f"q{n}": fact for n, fact in enumerate(facts)}
        questions = {
            name: Choice(instructions=question_text(fact),
                         criteria=ABSENCE_CRITERIA if fact["status"] == "absent" else PRESENT_CRITERIA)
            for name, fact in names.items()
        }
        orders = [passage_ids, list(reversed(passage_ids))][: max(1, self.settings.get("orders", 2))]
        runs = [self._ask(state(index, order), questions) for order in orders]
        out = {}
        for name, fact in names.items():
            answers = [run[name] for run in runs if name in run]
            if answers:
                out[fact["fact_id"]] = decision(average(answers), self.model)
        return out


class StubJev:
    """Deterministic stand-in for stub runs: confident 'supported' on near-total token overlap."""

    model = "stub-jev"

    def decide(self, facts: list[dict], index: PassageIndex, passage_ids: list[int]) -> dict[str, dict]:
        out = {}
        for fact in facts:
            if fact["status"] == "absent":
                probabilities = {"consistent": 0.6, "contradicted": 0.3, "unverifiable": 0.1}
            else:
                wanted = set(tokenize(fact["text"]))
                overlap = max((len(wanted & set(tokenize(index.passages[i]["text"]))) / max(len(wanted), 1)
                               for i in passage_ids), default=0.0)
                p = round(min(0.98, overlap), 4)
                probabilities = {"supported": p, "not_found": round(1 - p, 4), "contradicted": 0.0}
            out[fact["fact_id"]] = decision(probabilities, self.model)
        return out


def accepts(fact: dict, jev_decision: dict | None, codes: list[str], settings: dict) -> bool:
    """Cascade rule: accept JEV only for a confident pass the deterministic checks don't doubt."""
    return (
        settings.get("mode", "cascade") == "cascade"
        and (fact["status"] == "present" or settings.get("accept_absent", False))
        and jev_decision is not None
        and jev_decision["choice"] == ACCEPT[fact["status"]]
        and jev_decision["confidence"] >= settings["accept_confidence"]
        and not NO_ACCEPT_TRIGGERS & set(codes)
    )


def calibration_table(pairs: list[tuple[dict, str, str]], thresholds=None) -> list[dict]:
    """For each τ, the share JEV would accept and how often those disagree with the LLM.

    pairs: (jev decision, fact status, LLM verdict) for facts judged by both (shadow mode).
    """
    from qc.metrics import clopper_pearson

    thresholds = thresholds or [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98]
    rows = []
    for tau in thresholds:
        accepted = [(d, s, v) for d, s, v in pairs if d["choice"] == ACCEPT[s] and d["confidence"] >= tau]
        wrong = sum(1 for d, s, v in accepted if v != ACCEPT[s])
        rows.append({"tau": tau, "accept_share": len(accepted) / len(pairs) if pairs else None,
                     "accepted": len(accepted), "disagree": wrong,
                     "disagree_rate": wrong / len(accepted) if accepted else None,
                     "disagree_hi": clopper_pearson(wrong, len(accepted))[1] if accepted else None})
    return rows


def choose_tau(rows: list[dict], max_disagreement: float) -> float | None:
    """Smallest τ whose disagreement upper bound (95% Clopper-Pearson) stays under the limit."""
    ok = [r["tau"] for r in rows if r["accepted"] and r["disagree_hi"] is not None and r["disagree_hi"] <= max_disagreement]
    return min(ok) if ok else None

