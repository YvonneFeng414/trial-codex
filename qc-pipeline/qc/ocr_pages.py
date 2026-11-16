#!/usr/bin/env python3
"""OCR chosen pages of a PDF straight from their rendered image; print JSON {page: text}.

For pages whose text layer is glyph codes (a font without a usable ToUnicode map):
tools/ocr_pdf.py only OCRs pages that are near-empty or use fonts it recognizes as
garbled by glyph name, and these fonts pass that test. Rendering is unaffected by the
broken map, so OCR of the image recovers the text. macOS only (Apple Vision). Run with:
  uv run --with pypdfium2 --with ocrmac qc/ocr_pages.py IN.pdf PAGE [PAGE ...]
"""

from __future__ import annotations

import json
import sys

import pypdfium2 as pdfium
from ocrmac import ocrmac

DPI = 300
MAX_RENDER_SIDE = 5000  # as in tools/ocr_pdf.py: a poster-sized page needs no more


def page_text(page) -> str:
    width, height = page.get_size()
    scale = min(DPI / 72, MAX_RENDER_SIDE / max(width, height, 1))
    image = page.render(scale=scale).to_pil()
    results = ocrmac.OCR(image, recognition_level="accurate", language_preference=["en-US"]).recognize()
    # Each result is (text, confidence, [x, y, w, h]) in normalized coordinates with the
    # origin at the bottom left: read top to bottom, then left to right, one line each.
    lines = sorted(results, key=lambda r: (-round(r[2][1] + r[2][3], 2), r[2][0]))
    return "\n".join(text for text, _, _ in lines)


def main() -> int:
    pdf = pdfium.PdfDocument(sys.argv[1])
    out = {}
    for number in map(int, sys.argv[2:]):
        out[number] = page_text(pdf[number - 1])
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
