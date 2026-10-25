"""The random set R: a systematic draw over all facts with inclusion probability π (D3).

Facts are ordered by SAP section, then position. With u ~ U(0, 1) from the seed, fact k is
selected when ⌊(k+1)π + u⌋ > ⌊kπ + u⌋, so every fact has probability exactly π and
sections are covered in proportion to their size. Do not draw ⌈π·N_h⌉ per section: with
many small sections that over-samples (39% instead of 30% on LACE).
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

from qc.claims import section_rank


def ordered(facts: list[dict]) -> list[dict]:
    return sorted(facts, key=lambda f: (section_rank(f["section"]), f["md_line"], f["claim_id"], f["fact_id"]))


def systematic(n: int, pi: float, u: float) -> list[int]:
    """Positions 0..n-1 selected by the systematic rule; π is made exact as a fraction."""
    step = Fraction(pi).limit_denominator(10_000)
    start = Fraction(u)
    return [k for k in range(n) if math.floor((k + 1) * step + start) > math.floor(k * step + start)]


def draw(facts: list[dict], pi: float, seed: int) -> tuple[set[str], float]:
    """Return the fact ids in R and the start u (recorded with the results)."""
    u = random.Random(seed).random()
    sequence = ordered(facts)
    return {sequence[k]["fact_id"] for k in systematic(len(sequence), pi, u)}, u
