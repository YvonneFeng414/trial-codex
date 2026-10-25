"""SAP markdown -> claims, one per block that carries a *(Source: ...)* citation.

The block and citation parsers are the generator's own (tools/lint_sap.py), imported rather
than forked, so QC splits the SAP exactly as the linter does. Claim ids are 0-based over
cited blocks in document order, the numbering the LACE pilot used (C56, C96).
"""

from __future__ import annotations

import re

import qc  # noqa: F401  (puts tools/ on sys.path)
from lint_sap import (  # type: ignore[import-not-found]
    ABSENCE_RE,
    EXPECTED_H3,
    FIELD_LABEL_RE,
    SOURCE_RE,
    citation_units,
    substantive_blocks,
)

# `PDF 16` / `PDF pages 57, 62` is a page style some SAPs use; it never precedes a section number.
_PAGES_RE = re.compile(r"\b(?:pages?|pp?\.|PDF(?:\s+pages?)?)\s*", re.IGNORECASE)
_PAGE_LIST_RE = re.compile(
    r"(?P<list>\d+(?:\s*[-–—]\s*\d+)?(?:\s*(?:,|and|&)\s*\d+(?:\s*[-–—]\s*\d+)?)*)"
    r"(?P<sweep>\s*reviewed\b)?",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(r"(\d+)(?:\s*[-–—]\s*(\d+))?")
_OUTCOME_RE = re.compile(r"^\*\*Outcome:\s*(?P<name>.+?)\*\*")
_BOLD_ONLY_RE = re.compile(r"^\*\*(?P<label>[^*]+?):?\*\*$")
_SECTION_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")
_SECTION_ORDER = {title: index for index, title in enumerate(EXPECTED_H3)}


def parse_citation(citation: str) -> tuple[set[int], list[tuple[int, int]]]:
    """Return (specific pages, sweeps) from the inside of one *(Source: ...)*.

    Pages are read only after `page(s)` / `p.` / `pp.` / `PDF`, so section numbers such as
    `Sections 1.1–1.3` are never taken as pages. A range followed by `reviewed` is a sweep
    (`pages 1–96 reviewed`), kept apart from the pages that support a claim.
    """
    pages: set[int] = set()
    sweeps: list[tuple[int, int]] = []
    for part in citation.split(";"):
        for marker in _PAGES_RE.finditer(part):
            listed = _PAGE_LIST_RE.match(part, marker.end())
            if not listed:
                continue
            spans = []
            for match in _RANGE_RE.finditer(listed.group("list")):
                first = int(match.group(1))
                last = int(match.group(2) or first)
                spans.append((min(first, last), max(first, last)))
            if listed.group("sweep"):
                sweeps.extend(spans)
            else:
                for first, last in spans:
                    pages.update(range(first, last + 1))
    return pages, sweeps


def strip_citations(text: str) -> str:
    return re.sub(r"\s+", " ", SOURCE_RE.sub("", text)).strip()


def plain(text: str) -> str:
    """Claim text without list markers, bold markup or citations."""
    text = strip_citations(text)
    text = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text)
    return text.replace("**", "").strip()


def section_number(section: str) -> str:
    match = re.match(r"(\d+\.\d+)", section)
    return match.group(1) if match else ""


def section_rank(section: str) -> int:
    return _SECTION_ORDER.get(section, len(_SECTION_ORDER))


def _context(markdown: str) -> dict[int, dict[str, str | None]]:
    """Per markdown line: the ### section, the bold group label and the outcome block."""
    context: dict[int, dict[str, str | None]] = {}
    section: str | None = None
    group: str | None = None
    outcome: str | None = None
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        heading = _SECTION_RE.match(stripped)
        if heading:
            section, group, outcome = heading.group("title"), None, None
        elif stripped.startswith("## "):
            section, group, outcome = None, None, None
        else:
            outcome_match = _OUTCOME_RE.match(stripped)
            bold = _BOLD_ONLY_RE.match(stripped)
            if outcome_match:
                outcome, group = outcome_match.group("name").strip(), None
            elif bold:
                group = bold.group("label").strip()
        context[line_number] = {"section": section, "group": group, "outcome": outcome}
    return context


def parse(markdown: str, doc_id: str, max_page: int | None = None) -> list[dict]:
    """Split the SAP into claims. Uncited nested bullets join the cited block above them."""
    blocks = substantive_blocks(markdown)
    context = _context(markdown)
    claims: list[dict] = []
    for unit in citation_units(blocks):
        cited = [index for index, (_, text, _) in enumerate(unit) if SOURCE_RE.search(text)]
        owners: dict[int, int] = {}
        for index in range(len(unit)):
            if index in cited:
                owners[index] = index
            else:
                before = [c for c in cited if c < index]
                after = [c for c in cited if c > index]
                owners[index] = before[-1] if before else (after[0] if after else -1)
        groups: dict[int, list[int]] = {}
        for index, owner in owners.items():
            groups.setdefault(owner if owner >= 0 else index, []).append(index)
        for owner in sorted(groups):
            members = sorted(groups[owner])
            line_number, text, _ = unit[owner]
            lines = [unit[member][1] for member in members]
            citations = [c.group(0) for member in members for c in SOURCE_RE.finditer(unit[member][1])]
            citations = list(dict.fromkeys(citations))
            pages: set[int] = set()
            sweeps: list[tuple[int, int]] = []
            for citation in citations:
                found, swept = parse_citation(citation[len("*(Source:") : -2])
                pages |= found
                sweeps += swept
            where = context.get(line_number, {})
            section = where.get("section") or ""
            label = FIELD_LABEL_RE.match(text)
            outcome_line = _OUTCOME_RE.match(text.strip())
            if label:
                field = label.group("label").strip()
            elif outcome_line:
                field = "Outcome"
            else:
                field = where.get("group") or re.sub(r"^\d+\.\d+\s+", "", section)
            body = " ".join(plain(line) for line in lines)
            invalid = sorted(p for p in pages if max_page and (p < 1 or p > max_page))
            claims.append(
                {
                    "doc_id": doc_id,
                    "claim_id": -1,
                    "section": section,
                    "field": field,
                    "outcome_block": where.get("outcome"),
                    "text": body,
                    "lines": [plain(line) for line in lines],
                    "citations": citations,
                    "cited_pages": sorted(p for p in pages if p not in invalid),
                    "invalid_pages": invalid,
                    "sweep": [list(span) for span in sweeps],
                    "md_line": line_number,
                    "absence": bool(ABSENCE_RE.search(body)),
                    "cited": bool(citations),
                    # A citation that yields neither pages nor a sweep would otherwise pass
                    # silently as SWEEP_ONLY; it is counted in the run summary instead.
                    "citation_unparsed": bool(citations) and not pages and not sweeps and not invalid,
                }
            )
    claims.sort(key=lambda claim: claim["md_line"])
    for claim_id, claim in enumerate(claims):
        claim["claim_id"] = claim_id
    return claims
