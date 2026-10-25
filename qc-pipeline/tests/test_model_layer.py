"""Model-layer logic with a fake backend: quote re-ask, fixed-check overrides, atomizer checks."""

from __future__ import annotations

from qc import atomize
from qc.judge import AbsenceBatch, Judge, PresentBatch
from qc.packets import PassageIndex

CONFIG = {
    "packets": {"passage_words": 150, "passage_stride": 100},
    "judge": {"min_quote_chars": 10},
    "models": {"judge": "fake-judge"},
}
PAGES = [
    {"page": 18, "text": "The sample size of 755 patients gives 90% power with a 5% Type I error.",
     "clean": "The sample size of 755 patients gives 90% power with a 5% Type I error.", "unsearchable": False, "toc": False},
    {"page": 64, "text": "Non-inferiority if the lower bound is ≥-7%.",
     "clean": "Non-inferiority if the lower bound is ≥-7%.", "unsearchable": False, "toc": False},
]


class FakeBackend:
    config = CONFIG

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def parse(self, *, role, instructions, payload, schema, doc_id, stage, n_items):
        self.calls.append((role, stage, payload))
        return schema.model_validate({"verdicts": self.answers.pop(0)}) if schema in (PresentBatch, AbsenceBatch) \
            else schema.model_validate(self.answers.pop(0))


def _fact(fact_id, text, pages=(18,), status="present"):
    return {"fact_id": fact_id, "section": "2.3 Sample Size", "field": "Power", "text": text, "item": "power",
            "status": status, "cited_pages": list(pages), "outcome_block": None}


def _verdict(fact_id, verdict, quote, page, **extra):
    return {"fact_id": fact_id, "verdict": verdict, "minor": False, "quote": quote, "quote_page": page,
            "supporting_pages": [page] if page else [], "rationale": "r", **extra}


def _judge(answers):
    index = PassageIndex(PAGES, CONFIG)
    backend = FakeBackend(answers)
    return Judge(backend, PAGES, index, CONFIG, ["TLH", "TAH"], "doc"), backend, list(range(len(index.passages)))


def test_verified_quote_is_kept():
    judge, backend, ids = _judge([[_verdict("1.0", "supported", "755 patients gives 90% power", 18)]])
    out = judge.run_call([_fact("1.0", "755 patients with 90% power")], ids)["1.0"]
    assert out["verdict"] == "supported" and out["quote_verified"] and not out["needs_review"]
    assert len(backend.calls) == 1


def test_bad_quote_is_reasked_once_then_not_found():
    bad = _verdict("1.0", "supported", "755 patients give ninety percent power", 18)
    judge, backend, ids = _judge([[bad], [bad]])
    out = judge.run_call([_fact("1.0", "755 patients with 90% power")], ids)["1.0"]
    assert len(backend.calls) == 2 and "note" in backend.calls[1][2]
    assert out["verdict"] == "not_found" and out["raw_verdict"] == "supported" and out["needs_review"]


def test_quote_on_a_page_not_shown_fails():
    judge, _, _ = _judge([[_verdict("1.0", "supported", "755 patients gives 90% power", 18)]] * 2)
    page64_only = [i for i, p in enumerate(judge.index.passages) if p["page"] == 64]
    out = judge.run_call([_fact("1.0", "755 patients with 90% power")], page64_only)["1.0"]
    assert out["verdict"] == "not_found"


def test_fixed_checks_override_supported():
    judge, _, ids = _judge([[_verdict("1.0", "supported", "755 patients gives 90% power", 18),
                             _verdict("1.1", "supported", "the lower bound is ≥-7%", 64)]])
    out = judge.run_call([_fact("1.0", "755 patients with 80% power"),
                          _fact("1.1", "upper bound ≤ -7%", pages=(64,))], ids)
    assert out["1.0"]["override"] == ["NUM_MISS"] and out["1.0"]["needs_review"]
    assert out["1.1"]["override"] == ["POLARITY"]


def test_absence_contradiction_needs_verified_quote():
    judge, _, ids = _judge([[_verdict("2.0", "contradicted", "a 5% Type I error", 18)]])
    out = judge.run_call([_fact("2.0", "Not reported in protocol.", status="absent")], ids)["2.0"]
    assert out["verdict"] == "contradicted" and out["quote_verified"]


def test_atomizer_rejects_new_content_and_falls_back():
    claim = {"claim_id": 0, "doc_id": "d", "section": "2.3 Sample Size", "field": "Power", "outcome_block": None,
             "text": "Power: 90%.", "lines": ["Power: 90%."], "cited_pages": [18], "sweep": [], "md_line": 1,
             "absence": False}
    invented = {"claims": [{"claim_id": 0, "facts": [{"text": "90% power for TLH", "item": "power", "value": "90%", "status": "present"}]}]}
    assert atomize.check_fact("90% power for TLH", claim) == ["TLH"]
    assert atomize.check_fact("Power is 80%", claim) == ["0.8"]
    backend = FakeBackend([invented, invented])
    backend.config = {"models": {"atomizer": "fake", "judge": "fake"}}
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        facts = atomize.atomize([claim], backend, "d", Path(tmp), "sha")
    assert [f["source"] for f in facts] == ["rule_fallback"] and facts[0]["text"] == "90%."


def test_env_file_and_api_key_names(tmp_path, monkeypatch):
    import pytest

    from qc import llm
    from qc.config import ConfigError

    for name in ("OPENAI_API_KEY", "API_KEY", "OPENAI_API_URL", "QC_TEST_SHELL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nOPENAI_API_URL=https://example.test/v1\nexport API_KEY="sk-test"\nQC_TEST_SHELL=file\n')
    monkeypatch.setenv("QC_TEST_SHELL", "shell")
    monkeypatch.setattr(llm, "ENV_FILE", env)
    monkeypatch.setattr(llm.load_env, "__defaults__", (env,))
    assert llm.require_api_key() == "sk-test"
    import os

    assert os.environ["OPENAI_API_URL"] == "https://example.test/v1"
    assert os.environ["QC_TEST_SHELL"] == "shell"  # the shell wins over .env
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell")
    assert llm.require_api_key() == "sk-shell"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("API_KEY")
    env.write_text("OPENAI_API_URL=https://example.test/v1\n")
    with pytest.raises(ConfigError):
        llm.require_api_key()


def test_temperature_dropped_when_model_rejects_it(monkeypatch):
    import openai

    from qc import llm
    from qc.atomize import Atomization

    class BadRequestError(Exception):
        pass

    monkeypatch.setattr(openai, "BadRequestError", BadRequestError)
    monkeypatch.setattr(llm, "require_api_key", lambda: "sk-test")
    config = {"models": {"judge": "m", "atomizer": None, "forbidden": [], "temperature": 0, "max_output_tokens": 10}}
    backend = llm.OpenAIBackend(config, llm.CostLog(None))
    seen = []

    class Response:
        usage = None
        output_parsed = Atomization(claims=[])

    def fake_parse(**kwargs):
        seen.append("temperature" in kwargs)
        if "temperature" in kwargs:
            raise BadRequestError("Unsupported parameter: 'temperature' is not supported with this model")
        return Response()

    monkeypatch.setattr(backend.client.responses, "parse", fake_parse)
    for _ in range(2):
        backend.parse(role="judge", instructions="i", payload="p", schema=Atomization, doc_id="d", stage="s", n_items=0)
    assert seen == [True, False, False]  # dropped once, then remembered
