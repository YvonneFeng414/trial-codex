"""JEV front end: cascade rules, the SDK wrapper, and threshold calibration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qc import jev
from qc.judge import Judge, PresentBatch, AbsenceBatch
from qc.packets import PassageIndex

PAGES = [
    {"page": 18, "text": "The sample size of 755 patients gives 90% power with a 5% Type I error.",
     "clean": "The sample size of 755 patients gives 90% power with a 5% Type I error.", "unsearchable": False, "toc": False},
]
SETTINGS = {"enabled": True, "mode": "cascade", "model": "jev-test", "accept_confidence": 0.9, "orders": 2,
            "max_disagreement": 0.03}
CONFIG = {"packets": {"passage_words": 150, "passage_stride": 100}, "judge": {"min_quote_chars": 10},
          "models": {"judge": "llm-test"}, "jev": SETTINGS}


def _fact(fact_id, text="755 patients with 90% power", status="present"):
    return {"fact_id": fact_id, "section": "2.3 Sample Size", "field": "Power", "text": text, "item": "power",
            "status": status, "cited_pages": [18], "outcome_block": None}


class FixedFront:
    def __init__(self, decisions):
        self.decisions = decisions

    def decide(self, facts, index, passage_ids):
        return {f["fact_id"]: self.decisions[f["fact_id"]] for f in facts if f["fact_id"] in self.decisions}


class FakeLLM:
    config = CONFIG

    def __init__(self):
        self.asked = []

    def parse(self, *, role, instructions, payload, schema, doc_id, stage, n_items):
        import json

        ids = [f["fact_id"] for f in json.loads(payload)["facts"]]
        self.asked += ids
        rows = [{"fact_id": i, "verdict": "supported", "minor": False, "quote": "755 patients gives 90% power",
                 "quote_page": 18, "supporting_pages": [18], "rationale": "r"} for i in ids]
        if schema is AbsenceBatch:
            rows = [dict(r, verdict="consistent", quote="") for r in rows]
            for r in rows:
                r.pop("supporting_pages")
        return schema.model_validate({"verdicts": rows})


def _run(decisions, facts, codes=None, mode="cascade"):
    config = dict(CONFIG, jev=dict(SETTINGS, mode=mode))
    index = PassageIndex(PAGES, config)
    llm = FakeLLM()
    judge = Judge(llm, PAGES, index, config, [], "doc", front=FixedFront(decisions))
    return judge.run_call(facts, list(range(len(index.passages))), codes), llm


def _d(choice, confidence):
    return {"choice": choice, "confidence": confidence, "probabilities": {choice: confidence}, "model": "jev-test"}


def test_confident_supported_is_accepted_without_llm():
    out, llm = _run({"1.0": _d("supported", 0.97)}, [_fact("1.0")])
    assert out["1.0"]["judge_stage"] == "jev" and out["1.0"]["verdict"] == "supported"
    assert out["1.0"]["quote"] == "" and not out["1.0"]["quote_verified"] and llm.asked == []


@pytest.mark.parametrize("decision", [_d("supported", 0.6), _d("contradicted", 0.99), _d("not_found", 0.95)])
def test_low_confidence_and_errors_escalate(decision):
    out, llm = _run({"1.0": decision}, [_fact("1.0")])
    assert llm.asked == ["1.0"] and out["1.0"]["judge_stage"] == "llm"
    assert out["1.0"]["jev"] == decision and out["1.0"]["quote_verified"]


def test_deterministic_doubt_vetoes_acceptance():
    out, llm = _run({"1.0": _d("supported", 0.99)}, [_fact("1.0")], codes={"1.0": ["NUM_MISS", "CRITICAL"]})
    assert llm.asked == ["1.0"]
    out, llm = _run({"1.0": _d("supported", 0.99)}, [_fact("1.0")], codes={"1.0": ["CRITICAL", "COMPOUND"]})
    assert llm.asked == []
    out, llm = _run({"1.0": _d("supported", 0.99)}, [_fact("1.0")], codes={"1.0": ["SELF_CONTRA"]})
    assert llm.asked == ["1.0"]


def test_absence_facts_escalate_by_default():
    facts = [_fact("2.0", "Not reported in protocol.", "absent")]
    out, llm = _run({"2.0": _d("consistent", 0.99)}, facts)
    assert llm.asked == ["2.0"] and out["2.0"]["jev"]["choice"] == "consistent"


def test_absence_fact_accepts_consistent_only(monkeypatch):
    monkeypatch.setitem(SETTINGS, "accept_absent", True)
    facts = [_fact("2.0", "Not reported in protocol.", "absent")]
    out, llm = _run({"2.0": _d("consistent", 0.95)}, facts)
    assert out["2.0"]["verdict"] == "consistent" and llm.asked == []
    out, llm = _run({"2.0": _d("supported", 0.99)}, facts)  # wrong label set for an absence fact
    assert llm.asked == ["2.0"]


def test_shadow_mode_sends_everything_to_llm_and_keeps_jev():
    out, llm = _run({"1.0": _d("supported", 0.99), "1.1": _d("supported", 0.99)}, [_fact("1.0"), _fact("1.1")], mode="shadow")
    assert sorted(llm.asked) == ["1.0", "1.1"]
    assert all(v["judge_stage"] == "llm" and v["jev"]["confidence"] == 0.99 for v in out.values())


def test_sdk_wrapper_averages_both_orders(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def system_one(self, state, questions, model):
            calls.append({"order": [p["passage"] for p in state["protocol_passages"]], "questions": questions, "model": model})
            first = len(calls) == 1
            probs = {"supported": 0.9 if first else 0.7, "contradicted": 0.05, "not_found": 0.05 if first else 0.25}
            answers = {name: SimpleNamespace(probabilities=probs) for name in questions}
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=0), model=model,
                                   choices=answers)

    import typesafe_sdk

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeClient)
    monkeypatch.setattr(jev, "require_typesafe_key", lambda: "ts-test")
    from qc.llm import CostLog

    cost = CostLog(None)
    pages = PAGES + [dict(PAGES[0], page=19)]
    index = PassageIndex(pages, CONFIG)
    front = jev.JevFrontEnd(CONFIG, cost, "doc")
    out = front.decide([_fact("1.0"), _fact("2.0", "Not reported.", "absent")], index, [0, 1])
    assert [c["order"] for c in calls] == [["p18.0", "p19.0"], ["p19.0", "p18.0"]]
    assert set(calls[0]["questions"]["q1"].criteria) == set(jev.ABSENCE_CRITERIA)
    assert out["1.0"]["choice"] == "supported" and out["1.0"]["confidence"] == pytest.approx(0.8)
    assert len(cost.rows) == 2 and cost.rows[0]["stage"] == "jev"


def test_calibration_picks_smallest_safe_threshold():
    good = [(_d("supported", 0.95), "present", "supported")] * 200
    risky = [(_d("supported", 0.7), "present", "contradicted")] * 30 + [(_d("supported", 0.7), "present", "supported")] * 30
    table = jev.calibration_table(good + risky)
    by_tau = {row["tau"]: row for row in table}
    assert by_tau[0.9]["disagree"] == 0 and by_tau[0.5]["disagree"] == 30
    assert jev.choose_tau(table, 0.03) == 0.8
