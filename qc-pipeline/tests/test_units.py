"""Unit tests from implementation_plan.md section 6, plus the pilot bugs they encode."""

from __future__ import annotations

import random

import pytest

from qc import claims, pages, retrieve, sample, text, triggers
from qc.config import ConfigError, check_model


# --- citation parser ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "citation, pages_, sweeps",
    [
        ("Sections 1.1–1.3, pages 10–12", {10, 11, 12}, []),  # pilot bug: section numbers as pages
        ("Section 2.1, page 12; SAP Sections 1.1–1.2, pages 60–62", {12, 60, 61, 62}, []),
        ("Protocol synopsis, pages 8–9; Section 4.6, page 16", {8, 9, 16}, []),
        ("Protocol synopsis, pages 8-9, page 16", {8, 9, 16}, []),  # hyphen, two markers in one part
        ("pages 1–96 reviewed", set(), [(1, 96)]),  # sweep only
        ("Section 4.7, page 17; pages 1–96 reviewed", {17}, [(1, 96)]),
        ("SAP Sections 2.1, 2.7 and 2.10, pages 64, 76, 78–92", {64, 76, *range(78, 93)}, []),
        ("Sections 6.3–6.5 and 7.9, pages 22–23, 27–28", {22, 23, 27, 28}, []),
        ("SAP Sections 1.2 and 2.1, pages 62, 64", {62, 64}, []),
        ("Table 2, pp. 30-31", {30, 31}, []),
        ("Section 9.1, page 32 — DSMC", {32}, []),
        ("protocol §4.6, PDF 16; SAP cover and §1.2.1, PDF 57,62", {16, 57, 62}, []),
        ("SAP §2.1, PDF pages 63–64", {63, 64}, []),
    ],
)
def test_citation_parser(citation, pages_, sweeps):
    found, swept = claims.parse_citation(citation)
    assert found == pages_
    assert swept == sweeps


def test_claims_attach_uncited_nested_bullets():
    md = """# Statistical Analysis Plan

## 2. Study Methods

### 2.1 Trial Design

- **Intervention Summary:** Two arms. *(Source: Section 4, page 13)*
  - TAH: abdominal.
  - TLH: laparoscopic.
  - Radiotherapy is local. *(Source: Section 4.4, page 15)*
- **Design Type:** Not reported in protocol. *(Source: pages 1–20 reviewed)*
"""
    parsed = claims.parse(md, "doc", max_page=20)
    assert [c["md_line"] for c in parsed] == [7, 10, 11]
    assert parsed[0]["lines"][1:] == ["TAH: abdominal.", "TLH: laparoscopic."]
    assert parsed[0]["cited_pages"] == [13] and parsed[1]["cited_pages"] == [15]
    assert parsed[2]["absence"] and parsed[2]["sweep"] == [[1, 20]] and not parsed[2]["cited_pages"]
    assert triggers.arm_labels(parsed) == ["TAH", "TLH"]


def test_invalid_pages_are_separated():
    md = "### 1.1 Background and Rationale\nText. *(Source: Section 1, pages 5, 140)*\n"
    (claim,) = claims.parse(md, "doc", max_page=100)
    assert claim["cited_pages"] == [5] and claim["invalid_pages"] == [140]


# --- number normalizer -------------------------------------------------------------------

@pytest.mark.parametrize(
    "a, b",
    [(".05", "0.05"), ("0.05", "5%"), ("5 %", "0.05"), ("97.5%", "0.975"), ("1,000", "1000"),
     ("−7%", "-7%"), ("90 percent", "90%"), ("12.5 per cent", "0.125")],
)
def test_numbers_equal(a, b):
    assert text.numbers(a) == text.numbers(b) != set()


def test_numbers_drop_small_wholes_and_keep_mixed():
    assert text.numbers("a 2:1 ratio over 3 arms") == set()
    assert text.numbers("1,000 patients, 97.5% CI, 4.5 years, 755") == {1000.0, 0.975, 4.5, 755.0}
    assert text.numbers("10%") == {0.1}  # a percentage is never a trivial whole number
    assert text.numbers("1,2,3") == set()  # not a thousands separator


# --- RRF ---------------------------------------------------------------------------------

def test_rrf_ties_share_average_rank():
    fused = retrieve.rrf({"bm25": {1: 2.0, 2: 2.0, 3: 1.0}}, [1, 2, 3], k=60)
    assert fused[1] == fused[2] == pytest.approx(1 / 61.5)
    assert fused[3] == pytest.approx(1 / 63)


def test_rrf_all_zero_page_not_ahead_by_page_index():
    rankings = {"bm25": {1: 0.0, 2: 0.0, 3: 1.5}, "dense": {1: 0.0, 2: 0.4, 3: 0.0}}
    ranked = retrieve.order(retrieve.rrf(rankings, [1, 2, 3]), rankings)
    assert ranked[-1] == 1  # page 1 scores zero everywhere; its low index must not lift it


def test_rrf_number_ranker_skips_zero_pages():
    fused = retrieve.rrf({"num": {1: 0.0, 2: 1.0}}, [1, 2])
    assert fused[1] == 0.0 and fused[2] == pytest.approx(1 / 61)


# --- sampler -----------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 7, 33, 111, 229, 1000])
def test_sampler_size(n):
    for seed in range(50):
        chosen, _ = sample.draw([_fact(i) for i in range(n)], 0.3, seed)
        assert abs(len(chosen) - round(0.3 * n)) <= 1


def test_sampler_inclusion_probability_is_pi():
    n, seeds = 23, 10_000
    counts = [0] * n
    for seed in range(seeds):
        u = random.Random(seed).random()
        for k in sample.systematic(n, 0.3, u):
            counts[k] += 1
    assert all(abs(c / seeds - 0.3) < 0.02 for c in counts)


def test_sampler_is_systematic_not_per_section_ceiling():
    # 23 sections of 1-3 facts: ceil(0.3*N_h) per section would take far more than 30%.
    facts = [_fact(i, section=f"{1 + i // 3}.1 S") for i in range(60)]
    chosen, _ = sample.draw(facts, 0.3, 2026)
    assert len(chosen) == 18


def _fact(i: int, section: str = "2.3 Sample Size") -> dict:
    return {"fact_id": f"{i}.0", "claim_id": i, "section": section, "md_line": i}


# --- quote checker -----------------------------------------------------------------------

PAGE = ("Non-inferiority will be declared if the l ower bound of the one- sided 97.5% CI for the\n"
        "difference in dis ease -f re e survi val a t 4 .5 yea rs post-surgery is ≥-7%. The recruit-\n"
        "ment period is 60 months. The ﬁnal sample size was determined   by the trial statis t i c i a n s")


@pytest.mark.parametrize(
    "quote",
    [
        "the lower bound of the one-sided 97.5% CI",                 # broken letter spacing
        "disease-free survival at 4.5 years post-surgery is ≥−7%",    # minus sign, spacing
        "The recruitment period is 60 months",                        # hyphenation at line break
        "The final sample size was determined by the trial statisticians",  # ligature, spaces
        "Non-inferiority will be declared ... is ≥-7%",                # ellipsis joins pieces
    ],
)
def test_quote_matches(quote):
    assert text.quote_matches(quote, text.page_variants(PAGE))


@pytest.mark.parametrize("quote", ["the upper bound of the one-sided 97.5% CI", "is ≥-5%", "", "7%"])
def test_quote_rejects(quote):
    assert not text.quote_matches(quote, text.page_variants(PAGE))


# --- pages -------------------------------------------------------------------------------

def test_strip_line_numbers_including_glued():
    page = "\n".join([
        "Header text", "4.8 SAMPLE SIZE 466", "The final sample size is 755 patients 467",
        "versus 350 TAH 468", "  469", "assumes a ratio of 0.86:1 with 90% power, based on 60 470",
        " Primary Analysis 2.1.21471", "non-1472", "inferiority will be declared 1473",
    ])
    clean = pages.strip_line_numbers(page)
    assert "466" not in clean and "1473" not in clean and "1472" not in clean
    assert "based on 60" in clean and "755 patients" in clean and "Primary Analysis 2.1.2" in clean


def test_strip_line_numbers_on_table_heavy_page():
    numbered = [f"sentence {i} continues 18{i:02d} " for i in range(9, 21)]
    cells = ["All patients ", "N(%)  ", "Total ", "Difference ", "between ", " "] * 5
    clean = pages.strip_line_numbers("\n".join(numbered[:6] + cells + numbered[6:]))
    assert "1809" not in clean and "1820" not in clean and "sentence 9 continues" in clean


def test_strip_line_numbers_leaves_tables():
    page = "\n".join(["87% 54 54 0.76 730 415 315", "85% 54 54 0.86 705 380 325", "x"] * 3)
    assert pages.strip_line_numbers(page) == page


def test_toc_detection_with_margin_numbers():
    toc = "\n".join(f"{i}.1 HEADING ........................ {10 + i} {50 + i}" for i in range(10))
    assert pages.is_toc(toc)
    assert not pages.is_toc("Plain text.\nMore text with a number 12.\n")


# --- triggers ----------------------------------------------------------------------------

def test_polarity_conflicts():
    arms = ["TLH", "TAH"]
    assert triggers.polarity_conflicts("lower bound ≥ -7%", "the lower bound is ≤ -7%", arms) == ["direction"]
    assert triggers.polarity_conflicts("one-sided 97.5% CI", "two-sided 95% CI", arms) == ["sided"]
    assert triggers.polarity_conflicts("405 TLH patients", "350 TAH patients", arms) == ["arm"]
    assert triggers.polarity_conflicts("non-inferiority", "declare equivalence", arms) == ["hypothesis"]
    assert triggers.polarity_conflicts("non-inferiority/equivalence", "declare equivalence", arms) == []
    assert triggers.polarity_conflicts("superiority", "non-inferiority", arms) == ["hypothesis"]


def test_missing_numbers():
    assert triggers.missing_numbers("90% power, 755 patients", ["with 90% power ... 755 patients"]) == []
    assert triggers.missing_numbers("80% power", ["with 90% power"]) == [0.8]


# --- config ------------------------------------------------------------------------------

def test_judge_must_be_a_third_model():
    config = {"models": {"judge": "gpt-5.6-luna", "forbidden": ["gpt-5.6-terra", "gpt-5.6-luna"]}}
    with pytest.raises(ConfigError):
        check_model(config, "judge")
    config["models"]["judge"] = None
    with pytest.raises(ConfigError):
        check_model(config, "judge")
    config["models"]["judge"] = "some-third-model"
    assert check_model(config, "atomizer") == "some-third-model"


def test_non_independent_judge_needs_explicit_override():
    from qc.config import judge_independent

    config = {"models": {"judge": "gpt-5.6-luna", "atomizer": None, "forbidden": ["gpt-5.6-terra", "gpt-5.6-luna"],
                         "allow_non_independent_judge": True}}
    assert check_model(config, "judge") == "gpt-5.6-luna"
    assert judge_independent(config) is False
    config["models"].update(judge="some-third-model", allow_non_independent_judge=False)
    assert judge_independent(config) is True


# --- planted errors ----------------------------------------------------------------------

def test_plant_corruptions():
    from validate import plant

    rng = random.Random(1)
    fact = {"status": "present", "text": "lower bound ≥ -7% for 405 TLH under ITT", "item": "x", "field": "f"}
    assert plant.corrupt(fact, "direction_flip", rng, [])["text"] == "lower bound ≤ -7% for 405 TLH under ITT"
    assert "TAH" in plant.corrupt(fact, "arm_swap", rng, ["TLH", "TAH"])["text"]
    assert "per-protocol" in plant.corrupt(fact, "itt_to_pp", rng, [])["text"]
    scaled = plant.corrupt(fact, "scale", rng, [])["text"]
    assert scaled != fact["text"] and text.numbers(scaled) != text.numbers(fact["text"])
    assert plant.corrupt(fact, "to_absent", rng, [])["status"] == "absent"
