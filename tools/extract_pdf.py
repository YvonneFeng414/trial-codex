#!/usr/bin/env python3
"""Extract a PDF's text layer to an atomic, page-tagged UTF-8 file."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader


EXIT_ERROR = 1
EXIT_UNSUPPORTED_SCAN = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="input PDF")
    parser.add_argument("output", type=Path, help="page-tagged UTF-8 output")
    parser.add_argument(
        "--min-chars-per-page",
        type=int,
        default=20,
        help="minimum average non-whitespace characters per page (default: 20)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_chars_per_page < 0:
        print("error: --min-chars-per-page must be non-negative", file=sys.stderr)
        return EXIT_ERROR
    if not args.pdf.is_file():
        print(f"error: PDF not found: {args.pdf}", file=sys.stderr)
        return EXIT_ERROR

    try:
        reader = PdfReader(args.pdf)
        page_count = len(reader.pages)
    except Exception as exc:
        print(f"error: cannot open PDF {args.pdf}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if page_count == 0:
        print(f"error: PDF has no pages: {args.pdf}", file=sys.stderr)
        return EXIT_ERROR

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    total_chars = 0
    marker_count = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{args.output.name}.",
            suffix=".tmp",
            dir=args.output.parent,
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            for index, page in enumerate(reader.pages, start=1):
                try:
                    text = (page.extract_text() or "").strip()
                except Exception as exc:
                    raise RuntimeError(f"page {index}: {exc}") from exc
                stream.write(f"===== PAGE {index} OF {page_count} =====\n")
                stream.write(text)
                stream.write("\n\n")
                marker_count += 1
                total_chars += sum(not char.isspace() for char in text)
            stream.flush()
            os.fsync(stream.fileno())

        if marker_count != page_count:
            raise RuntimeError(
                f"extracted {marker_count} page markers for {page_count} PDF pages"
            )

        minimum_chars = page_count * args.min_chars_per_page
        if total_chars < minimum_chars:
            temp_path.unlink(missing_ok=True)
            print(
                "unsupported scan: PDF has too little extractable text "
                f"({total_chars} non-whitespace characters across {page_count} pages; "
                f"minimum {minimum_chars}). OCR is required: {args.pdf}",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED_SCAN

        os.replace(temp_path, args.output)
        print(
            f"extracted {page_count} pages and {total_chars} non-whitespace characters "
            f"to {args.output}"
        )
        return 0
    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        print(f"error: failed to extract {args.pdf}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
