#!/usr/bin/env python3
"""Deterministic structural and grounding checks for a generated SAP."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


BANNED_PHRASES = (
    "likely",
    "implied",
    "presumably",
    "typically",
    "would be",
    "standard practice",
    "governed by SOPs",
)
BANNED_RE = re.compile(
    r"\b(" + "|".join(re.escape(item) for item in BANNED_PHRASES) + r")\b",
    re.IGNORECASE,
)
SOURCE_RE = re.compile(r"\*\(Source:\s*[^)]+\)\*")
PLACEHOLDER_RE = re.compile(
    r"(?:<\s*(?:value|section|page|name)[^>]*>|\b(?:TODO|TBD|Not yet reached)\b)",
    re.IGNORECASE,
)
FIELD_LABEL_RE = re.compile(r"^[-*+]\s+\*\*(?P<label>[^*]+?):?\*\*")
# Free-form hedges count as absence too: a field that says the test "is not reported"
# claims the same gap as one that says "Not reported in protocol".
ABSENCE_RE = re.compile(
    r"\bnot (?:reported|applicable|specified|stated|named|described)\b"
    r"|\bdoes not (?:report|specify|state|name|describe)\b",
    re.IGNORECASE,
)

# Topics that a field can only be absent for if no other field answers them. Field-by-
# field review is blind to contradictions that span fields: a draft can call the
# multiplicity strategy unreported in 3.1 while spelling out the gatekeeping sequence in
# 2.4, and every per-field check still passes.
FIELD_TOPICS = (
    (
        "Multiplicity Adjustment",
        (
            r"gatekeep",
            r"\bstep-down\b",
            r"\bclosed testing\b",
            r"\balpha[- ]spending\b",
            r"\bBonferroni\b",
            r"\bHolm\b",
            r"\bfamily-?wise\b",
            r"\btesting (?:hierarchy|sequence)\b",
            r"\bhierarchical(?:ly)? test",
            r"\bsequential(?:ly)? test",
            r"\bconditional (?:comparison|test)",
            r"\bfollow(?:s|ed by) (?:a |the )?(?:positive|success)",
        ),
    ),
    (
        "Statistical Test Assumed",
        (
            r"\bFisher(?:['’]s)? exact\b",
            r"\bANCOVA\b",
            r"\bANOVA\b",
            r"\blog-?rank\b",
            r"\bchi-?squared?\b",
            r"\bt-tests?\b",
            r"\bWilcoxon\b",
            r"\bMann-?Whitney\b",
            r"\blogistic regression\b",
            r"\bCox (?:model|regression|proportional)",
        ),
    ),
    (
        "Statistical Software",
        (
            r"\bSAS\b",
            r"\bnQuery\b",
            r"\bNONMEM\b",
            r"\bEAST\s+\d",
            r"\bStata\b",
            r"\bSPSS\b",
            r"\bR version\b",
        ),
    ),
    (
        "Interim Analyses Planned",
        (
            r"\binterim analys",
            r"\bDSMB\b",
            r"\bdata (?:and )?safety monitoring\b",
            r"\bunblinded (?:review|analysis)\b",
        ),
    ),
    (
        "Covariate Adjustment",
        (r"\bcovariate\b", r"\badjusted for\b", r"\bANCOVA\b"),
    ),
    (
        "Subgroup Analyses",
        (r"\bby age (?:stratum|strata|group)\b", r"\bstratified analys", r"\bwithin each stratum\b"),
    ),
    (
        "Handling Methods",
        (
            r"\bimputation\b",
            r"\bLOCF\b",
            r"\bBOCF\b",
            r"\bobserved[- ]case\b",
            r"\bcarried forward\b",
        ),
    ),
    (
        "Missing Data Assumptions",
        (r"\bMN?AR\b", r"\bMCAR\b", r"\bmissing (?:completely )?at random\b"),
    ),
)

EXPECTED_H2 = (
    "1. Introduction",
    "2. Study Methods",
    "3. Statistical Principles",
    "4. Trial Population",
    "5. Analysis",
)
EXPECTED_H3 = (
    "1.1 Background and Rationale",
    "1.2 Objectives",
    "2.1 Trial Design",
    "2.2 Randomization",
    "2.3 Sample Size",
    "2.4 Hypothesis Framework",
    "2.5 Statistical Interim Analyses",
    "2.6 Timing of Analyses",
    "3.1 Significance and Confidence Intervals",
    "3.2 Protocol Deviations",
    "3.3 Analysis Populations",
    "4.1 Screening Data",
    "4.2 Eligibility Criteria Summary",
    "4.3 Recruitment",
    "4.4 Withdrawal and Follow-up",
    "4.5 Baseline Characteristics",
    "5.1 Primary Outcomes",
    "5.2 Secondary Outcomes",
    "5.3 Analysis Methods",
    "5.4 Missing Data",
    "5.5 Additional Analyses",
    "5.6 Harms",
    "5.7 Software and References",
)
REQUIRED_LABELS = (
    "Primary Objectives",
    "Secondary Objectives",
    "Exploratory Objectives",
    "Design Type",
    "Allocation Ratio",
    "Intervention Summary",
    "Method",
    "Stratification Factors",
    "Minimization or Blocking",
    "Planned Sample Size",
    "Outcome Used for Calculation",
    "Outcome Type",
    "Alpha (Significance Level)",
    "Power",
    "Statistical Test Assumed",
    "Missing Data Assumptions",
    "Handling Methods",
    "Primary Analysis Methods",
    "Sensitivity Analyses",
    "Adverse Event Definitions",
    "Safety Analysis Methods",
    "Statistical Software",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path, help="SAP Markdown file")
    parser.add_argument(
        "--max-page",
        type=int,
        help="reject explicit p./pp. citations above this protocol page count",
    )
    return parser.parse_args()


def heading_violations(markdown: str, level: int, expected: tuple[str, ...]) -> list[str]:
    prefix = "#" * level + " "
    found: list[tuple[int, str]] = []
    in_fence = False
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith(prefix) and not line.startswith(prefix + "#"):
            found.append((line_number, re.sub(r"\s+", " ", line[len(prefix) :]).strip()))

    violations: list[str] = []
    titles = [title for _, title in found]
    for line_number, title in found:
        if title not in expected:
            violations.append(
                f'line {line_number}: unexpected {prefix.strip()} heading "{title}"'
            )
        elif titles.count(title) > 1 and titles.index(title) == found.index((line_number, title)):
            violations.append(f'duplicate heading "{prefix}{title}"')
    for title in expected:
        if title not in titles:
            violations.append(f'missing heading "{prefix}{title}"')
    recognized = [title for title in titles if title in expected]
    expected_present = [title for title in expected if title in recognized]
    if recognized != expected_present:
        violations.append(f"{prefix.strip()} headings are out of order")
    return violations


def substantive_blocks(markdown: str) -> list[tuple[int, str, int]]:
    """Return prose paragraphs and list items with their indent, outside comments/fences.

    Indent is 0 for paragraphs and top-level list items and the leading-space count for
    nested items, which lets a nested run share its parent field's citation.
    """
    blocks: list[tuple[int, str, int]] = []
    paragraph: list[str] = []
    paragraph_start = 0
    in_fence = False
    in_comment = False

    def flush() -> None:
        nonlocal paragraph, paragraph_start
        if paragraph:
            blocks.append((paragraph_start, " ".join(part.strip() for part in paragraph), 0))
            paragraph = []
            paragraph_start = 0

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        if "<!--" in stripped:
            flush()
            in_comment = True
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            flush()
            continue
        if stripped.startswith(("#", "---")):
            flush()
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", stripped):
            flush()
            continue
        if re.match(r"^(?:[-*+] |\d+[.)] )", stripped):
            flush()
            blocks.append((line_number, stripped, len(line) - len(line.lstrip())))
            continue
        if paragraph_start == 0:
            paragraph_start = line_number
        paragraph.append(stripped)
    flush()
    return blocks


def citation_units(blocks: list[tuple[int, str, int]]) -> list[list[tuple[int, str, int]]]:
    """Group each top-level block with the nested list items that follow it.

    One citation covers a unit, so enumerating trial arms or objectives as sub-bullets
    costs no more citations than collapsing them into a single sentence. Sibling
    top-level fields stay in separate units and still need a citation each.
    """
    units: list[list[tuple[int, str, int]]] = []
    index = 0
    while index < len(blocks):
        unit = [blocks[index]]
        top_level = blocks[index][2] == 0
        index += 1
        if top_level:
            while index < len(blocks) and blocks[index][2] > 0:
                unit.append(blocks[index])
                index += 1
        units.append(unit)
    return units


def cross_field_violations(blocks: list[tuple[int, str, int]]) -> list[str]:
    """Flag a field marked absent while another field answers the same topic."""
    violations: list[str] = []
    for label, patterns in FIELD_TOPICS:
        owner: tuple[int, str] | None = None
        for line_number, text, _ in blocks:
            match = FIELD_LABEL_RE.match(text)
            if match and match.group("label").strip() == label:
                owner = (line_number, text)
                break
        if owner is None or not ABSENCE_RE.search(owner[1]):
            continue

        topic_re = re.compile("|".join(patterns), re.IGNORECASE)
        # A field that already names part of its own topic ("No imputation is performed;
        # per-endpoint rules are not reported") is scoping, not claiming a blanket gap.
        if topic_re.search(owner[1]):
            continue

        for line_number, text, _ in blocks:
            if line_number == owner[0] or ABSENCE_RE.search(text):
                continue
            hit = topic_re.search(text)
            if hit:
                violations.append(
                    f'line {owner[0]}: "{label}" is marked absent, but line {line_number} '
                    f'states "{hit.group(0)}" - fill the field from that source, or state '
                    f"the scope that separates the two"
                )
                break
    return violations


def lint(markdown: str, max_page: int | None) -> list[str]:
    violations: list[str] = []
    if not markdown.strip():
        return ["draft is empty"]
    if not re.search(r"^# Statistical Analysis Plan\s*$", markdown, re.MULTILINE):
        violations.append('missing title "# Statistical Analysis Plan"')
    violations.extend(heading_violations(markdown, 2, EXPECTED_H2))
    violations.extend(heading_violations(markdown, 3, EXPECTED_H3))
    for label in REQUIRED_LABELS:
        if not re.search(rf"\*\*{re.escape(label)}:\?\*\*", markdown):
            # Objective labels omit a colon; field labels include it inside bold text.
            if f"**{label}**" not in markdown and f"**{label}:**" not in markdown:
                violations.append(f'missing required field "{label}"')

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        match = BANNED_RE.search(line)
        if match:
            violations.append(f'line {line_number}: banned phrase "{match.group(0)}"')
        placeholder = PLACEHOLDER_RE.search(line)
        if placeholder:
            violations.append(f'line {line_number}: placeholder "{placeholder.group(0)}"')

    blocks = substantive_blocks(markdown)
    for unit in citation_units(blocks):
        if not any(SOURCE_RE.search(text) for _, text, _ in unit):
            violations.append(f"line {unit[0][0]}: substantive block lacks *(Source: ...)*")
    violations.extend(cross_field_violations(blocks))

    if max_page is not None:
        if max_page < 1:
            violations.append("--max-page must be positive")
        else:
            cited_pages: list[tuple[str, int]] = []
            for match in re.finditer(r"\bpp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?", markdown, re.I):
                cited_pages.append((match.group(0), int(match.group(2) or match.group(1))))
            for match in re.finditer(r"\bPages?\s+(\d+)(?:\s*[-–]\s*(\d+))", markdown, re.I):
                cited_pages.append((match.group(0), int(match.group(2) or match.group(1))))
            for citation, page in cited_pages:
                if page > max_page:
                    violations.append(
                        f'citation "{citation}" exceeds protocol page count {max_page}'
                    )
    return violations


def main() -> int:
    args = parse_args()
    if not args.draft.is_file():
        print(f"error: draft not found: {args.draft}", file=sys.stderr)
        return 2
    try:
        markdown = args.draft.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.draft}: {exc}", file=sys.stderr)
        return 2

    violations = lint(markdown, args.max_page)
    if violations:
        print(f"FAIL: {len(violations)} violation(s)", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print(f"PASS: {args.draft}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
