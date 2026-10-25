#!/usr/bin/env python3
"""Record the largest page number cited in each SAP's `*(Source: ...)*` citations."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


SOURCE_RE = re.compile(r"\(Source:(.*?)\)\*", re.DOTALL)
# A page keyword opens a list of numbers: "page 10", "pages 4-5, 7 and 9", "pp. 12–14",
# "pages96–97". Numbers elsewhere in a citation are section, table, or version numbers.
PAGE_KEYWORD_RE = re.compile(r"(?<![A-Za-z])(?:pages?|pgs?\.?|pp?\.?)\s*(?=\d)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")
LIST_SEPARATOR_RE = re.compile(
    r"\s*(?:,|-|–|—|‑|−|\bto\b|\band\b|&|\bthrough\b)\s*(?:and\s+)?",
    re.IGNORECASE,
)
# The document-wide range that backs an absence claim: "pages 1-105 reviewed".
REVIEWED_RE = re.compile(r"pages?\s*\d+\s*[-–—‑−]\s*(\d+)\s*reviewed", re.IGNORECASE)
FIELDNAMES = ("label", "id", "file", "max_page", "num_citations", "reviewed_max_page", "note")


def cited_pages(citation: str) -> list[int]:
    pages: list[int] = []
    for keyword in PAGE_KEYWORD_RE.finditer(citation):
        position = keyword.end()
        while True:
            number = NUMBER_RE.match(citation, position)
            if number is None:
                break
            # "pages 10, 12.3 Analysis": a decimal is a section number, not a page.
            if re.match(r"\.\d", citation[number.end() : number.end() + 2]):
                break
            pages.append(int(number.group()))
            separator = LIST_SEPARATOR_RE.match(citation, number.end())
            if separator is None:
                break
            position = separator.end()
    return pages


def summarize(markdown: str) -> dict[str, object]:
    citations = SOURCE_RE.findall(markdown)
    pages = [page for citation in citations for page in cited_pages(citation)]
    reviewed = [int(end) for citation in citations for end in REVIEWED_RE.findall(citation)]
    max_page = max(pages) if pages else None
    reviewed_max = max(reviewed) if reviewed else None
    notes: list[str] = []
    if not citations:
        notes.append("no Source citations")
    elif max_page is None:
        notes.append("no page numbers in citations")
    elif reviewed_max is not None and max_page > reviewed_max:
        notes.append(f"max_page exceeds reviewed range 1-{reviewed_max}")
    return {
        "max_page": "" if max_page is None else max_page,
        "num_citations": len(citations),
        "reviewed_max_page": "" if reviewed_max is None else reviewed_max,
        "note": "; ".join(notes),
    }


def manifest_ids(root: Path) -> dict[tuple[str, str], str]:
    manifest = root / "manifest.csv"
    if not manifest.is_file():
        return {}
    with manifest.open(newline="", encoding="utf-8") as handle:
        return {(row["label"], row["file"]): row["id"] for row in csv.DictReader(handle)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory of <label>/<sap>.md files")
    parser.add_argument("-o", "--output", type=Path, required=True, help="CSV path to write")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = sorted(args.root.glob("*/*.md"))
    if not paths:
        print(f"no markdown files under {args.root}", file=sys.stderr)
        return 1

    ids = manifest_ids(args.root)
    rows = []
    for path in paths:
        label = path.parent.name
        row = {"label": label, "id": ids.get((label, path.name), ""), "file": path.name}
        row.update(summarize(path.read_text(encoding="utf-8", errors="replace")))
        rows.append(row)

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    flagged = [row for row in rows if row["note"]]
    print(f"wrote {len(rows)} rows to {args.output} ({len(flagged)} flagged)")
    for row in flagged:
        print(f'  {row["label"]}/{row["file"]}: {row["note"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
