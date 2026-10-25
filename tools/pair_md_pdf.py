#!/usr/bin/env python3
"""Pair every benchmark protocol markdown with the raw PDF it was extracted from.

Each pair is copied side by side into `<out-dir>/<doi>/` as `<stem>.md` and `<stem>.pdf`.

Markdown names follow `<journal>++<doi_dir>++<stem>.md`; the PDF lives at
`<pdf-root>/<journal>/<doi_dir>/<stem>.pdf`. BMJ folders write the DOI with `:` where
the markdown name has the first `_` (`10.1136_bmj-2022-071245` -> `10.1136:bmj-2022-071245`).
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import Counter
from pathlib import Path


DEFAULT_MD_ROOT = Path("Benchmark_merged")
DEFAULT_PDF_ROOT = Path("/Users/yixuanfeng/Desktop/web-download/downloads-clean")
DEFAULT_OUT_DIR = Path("Benchmark_paired")
FIELDNAMES = ("label", "journal", "doi_dir", "md_path", "pdf_path", "match_rule")


def candidate_dirs(doi_dir: str) -> list[tuple[str, str]]:
    return [("exact", doi_dir), ("bmj_colon", doi_dir.replace("_", ":", 1))]


def pair(md_path: Path, pdf_root: Path) -> dict[str, str]:
    journal, doi_dir, stem = md_path.stem.split("++", 2)
    row = {
        "label": md_path.parent.name,
        "journal": journal,
        "doi_dir": doi_dir,
        "md_path": str(md_path),
        "pdf_path": "",
        "match_rule": "missing",
    }
    for rule, directory in candidate_dirs(doi_dir):
        pdf_path = pdf_root / journal / directory / f"{stem}.pdf"
        if pdf_path.is_file():
            row["pdf_path"] = str(pdf_path)
            row["match_rule"] = rule
            break
    return row


def copy_if_changed(source: Path, target: Path) -> None:
    if target.is_file() and target.stat().st_size == source.stat().st_size:
        return
    shutil.copy2(source, target)


def materialize(row: dict[str, str], out_dir: Path) -> dict[str, str]:
    """Copy one pair to `<out_dir>/<doi>/<stem>.{md,pdf}`; return the row with paths relative to out_dir.

    A few DOIs carry two protocols (e.g. protocol + appendix); both pairs share the DOI
    directory and are told apart by stem.
    """
    md_path = Path(row["md_path"])
    stem = md_path.stem.split("++", 2)[2]
    doi = row["doi_dir"].strip()
    pair_dir = out_dir / doi
    pair_dir.mkdir(parents=True, exist_ok=True)
    copied = dict(row, source_md_path=row["md_path"], source_pdf_path=row["pdf_path"])
    copy_if_changed(md_path, pair_dir / f"{stem}.md")
    copied["md_path"] = str(Path(doi) / f"{stem}.md")
    if row["pdf_path"]:
        copy_if_changed(Path(row["pdf_path"]), pair_dir / f"{stem}.pdf")
        copied["pdf_path"] = str(Path(doi) / f"{stem}.pdf")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--md-root", type=Path, default=DEFAULT_MD_ROOT)
    parser.add_argument("--pdf-root", type=Path, default=DEFAULT_PDF_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="each pair is copied to <out-dir>/<doi>/<stem>.{md,pdf}, with pairs.csv at the top")
    args = parser.parse_args()

    md_paths = sorted(args.md_root.glob("*/*.md"))
    rows = [materialize(pair(md_path, args.pdf_root), args.out_dir) for md_path in md_paths]

    out_csv = args.out_dir / "pairs.csv"
    with out_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES + ("source_md_path", "source_pdf_path"))
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["match_rule"] for row in rows)
    print(f"{len(rows)} markdowns -> {out_csv}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    missing = [row["md_path"] for row in rows if row["match_rule"] == "missing"]
    for md_path in missing:
        print(f"missing PDF: {md_path}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
