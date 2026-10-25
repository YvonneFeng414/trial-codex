"""Regression fixture: LACE (10.1001_jama.2017.2068), the pilot document.

The deterministic layer runs with no model tokens. The pilot's verdicts need a real judge,
so those checks run only when QC_JUDGE_TESTS=1 and models.judge is configured.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from qc import REPO_ROOT
from qc.config import load as load_config

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lace_pilot.json").read_text(encoding="utf-8"))
DOC = REPO_ROOT / FIXTURE["doc"]
pytestmark = pytest.mark.skipif(not DOC.is_dir(), reason="LACE pair not present")


@pytest.fixture(scope="module")
def lace(tmp_path_factory):
    from qc.run_qc import pair_from_dir, run_document
    from qc.llm import CostLog

    out = tmp_path_factory.mktemp("qc_out")
    args = Namespace(out=out, stage="deterministic", backend="stub", atomizer="rule", dense="tfidf",
                     lexicon=None, perturb=True, ocr=False)
    pair = pair_from_dir(DOC, None)
    row, cover_rows = run_document(pair, load_config(), args, CostLog(None))
    work = out / pair.doi_dir
    read = lambda name: [json.loads(l) for l in open(work / f"{pair.stem}.{name}.jsonl", encoding="utf-8")]  # noqa: E731
    return {"row": row, "cover": json.loads((work / f"{pair.stem}.coverage.json").read_text()),
            "claims": read("claims"), "facts": read("facts")}


def test_claim_counts(lace):
    claims = lace["claims"]
    assert len(claims) == FIXTURE["claims"]
    assert sum(bool(c["cited_pages"]) for c in claims) == FIXTURE["claims_with_specific_pages"]
    assert lace["row"]["n_pages"] == FIXTURE["pages"]
    assert lace["claims"][56]["section"] == "4.3 Recruitment"
    assert lace["claims"][96]["field"] == "Transformations / Derived Variables"


def test_no_tokens_in_deterministic_stage(lace):
    assert lace["row"]["input_tokens"] == 0 and lace["row"]["output_tokens"] == 0


def test_recall_reproduces_pilot(lace):
    recall = lace["cover"]["claims"]["recall"]
    assert recall["5"] >= FIXTURE["pilot_recall_at_5_claims"] - 0.02
    assert lace["cover"]["claims"]["baseline"]["5"] < 0.25


def test_perturbation_barely_moves_recall(lace):
    perturbed = lace["cover"]["perturbation"]
    assert perturbed["n"] >= 30
    assert perturbed["original_recall"] - perturbed["perturbed_recall"] <= 0.10


@pytest.mark.parametrize("claim_id", sorted(FIXTURE["expected_triggers"]))
def test_pilot_errors_are_flagged(lace, claim_id):
    codes = {code for f in lace["facts"] if f["claim_id"] == int(claim_id) and f["status"] == "absent"
             for code in f["triggers"]}
    assert set(FIXTURE["expected_triggers"][claim_id]) <= codes


def test_toc_pages_excluded(lace):
    assert lace["row"]["toc_pages"] >= 3


@pytest.mark.skipif(os.environ.get("QC_JUDGE_TESTS") != "1", reason="needs a configured judge model")
def test_pilot_verdicts_with_judge(tmp_path):
    from qc.run_qc import main

    assert main(["--doc", str(DOC), "--out", str(tmp_path), "--backend", "openai"]) == 0
    work = next(tmp_path.glob("*/"))
    verdicts = {v["fact_id"]: v for v in map(json.loads, open(next(work.glob("*.verdicts.jsonl"))))}
    assert all(v["quote_verified"] for v in verdicts.values() if v["verdict"] in ("supported", "contradicted"))
    by_claim = {}
    for fact_id, verdict in verdicts.items():
        by_claim.setdefault(fact_id.split(".")[0], []).append(verdict["verdict"])
    assert "contradicted" in by_claim["96"]
