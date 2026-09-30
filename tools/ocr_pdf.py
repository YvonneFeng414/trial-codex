#!/usr/bin/env python3
"""Add an invisible OCR text layer to the scanned pages of a PDF.

Pages whose text layer is thinner than --min-chars-per-page are rendered and
recognized with Apple Vision (ocrmac). The recognized lines are drawn over the
page image as invisible text (render mode 3), the way ocrmypdf does it, so
tools/extract_pdf.py reads the result like any born-digital PDF. Pages that
already carry text are copied through untouched. Page count and order never
change, so a page number in the searchable copy is the same page in the original.

macOS only. Run with:
  uv run --with pypdf --with pypdfium2 --with ocrmac --with reportlab \\
      tools/ocr_pdf.py IN.pdf OUT.pdf
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
from pathlib import Path

import pypdfium2 as pdfium
from ocrmac import ocrmac
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


EXIT_ERROR = 1
EXIT_UNSUPPORTED_SCAN = 3

# Base-14 Helvetica cannot encode >=, <=, and friends, which protocols use
# constantly in eligibility criteria and thresholds. An embedded Unicode TTF
# keeps them extractable; reportlab subsets it, so only used glyphs are stored.
FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
FONT_NAME = "OCRUnicode"

# Longest rendered side in pixels. A poster-sized page at --dpi would otherwise
# allocate a bitmap far past what Vision needs to read body text.
MAX_RENDER_SIDE = 5000

# Vision observations below this confidence are recognized again from a crop.
# Clean print scores 1.0; the fused-line failure this catches scores about 0.3.
LOW_CONFIDENCE = 0.5
# Crop padding around a retried box, as a fraction of the page.
RETRY_PAD = 0.01


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdf", type=Path, help="input PDF")
    parser.add_argument("output", type=Path, help="searchable output PDF")
    parser.add_argument(
        "--min-chars-per-page",
        type=int,
        default=20,
        help="OCR a page whose text layer has fewer non-whitespace characters, and "
        "require this average after OCR, as tools/extract_pdf.py does (default: 20)",
    )
    parser.add_argument("--dpi", type=int, default=300, help="render resolution (default: 300)")
    parser.add_argument(
        "--level",
        choices=("accurate", "fast"),
        default="accurate",
        help="Vision recognition level (default: accurate)",
    )
    parser.add_argument("--lang", default="en-US", help="Vision language preference (default: en-US)")
    parser.add_argument(
        "--all-pages", action="store_true", help="OCR every page, even ones with a text layer"
    )
    return parser.parse_args()


def nonspace(text: str) -> int:
    return sum(not char.isspace() for char in text)


def group_lines(results) -> list[list[tuple[float, float, float, float, str]]]:
    """Vision observations -> reading-order lines of (x, y, w, h, text) runs.

    Coordinates are normalized with a bottom-left origin. Runs whose vertical
    centers sit within half a run height of each other share a line; a table row
    split into cells comes back as one line, left to right.
    """
    runs = sorted(
        ((x, y, w, h, text) for text, _conf, (x, y, w, h) in results if text.strip()),
        key=lambda run: -(run[1] + run[3] / 2),
    )
    lines: list[list[tuple[float, float, float, float, str]]] = []
    centers: list[float] = []
    for run in runs:
        center = run[1] + run[3] / 2
        if lines and abs(centers[-1] - center) <= 0.5 * min(run[3], lines[-1][0][3]):
            lines[-1].append(run)
        else:
            lines.append([run])
            centers.append(center)
    for line in lines:
        line.sort(key=lambda run: run[0])
    return lines


def overlap(inner, outer) -> float:
    """Share of INNER's box area that lies inside OUTER's (normalized x, y, w, h)."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    dx = min(ix + iw, ox + ow) - max(ix, ox)
    dy = min(iy + ih, oy + oh) - max(iy, oy)
    if dx <= 0 or dy <= 0 or iw * ih <= 0:
        return 0.0
    return dx * dy / (iw * ih)


def recognize_page(pdf_page, level: str, lang: str, dpi: int):
    width, height = pdf_page.get_size()
    scale = min(dpi / 72, MAX_RENDER_SIDE / max(width, height))
    image = pdf_page.render(scale=scale, grayscale=True).to_pil()

    def recognize(img):
        return ocrmac.OCR(img, recognition_level=level, language_preference=[lang]).recognize()

    results = recognize(image)
    kept = [obs for obs in results if obs[1] >= LOW_CONFIDENCE]

    # Vision occasionally fuses two lines into one box (an underlined link is a
    # reliable trigger) and returns gibberish for it at low confidence. The same
    # region recognized on its own reads cleanly, so retry each such box as a
    # padded crop, map the crop's boxes back onto the page, and drop any that
    # repeat a neighboring line the padding pulled in.
    for obs in results:
        if obs[1] >= LOW_CONFIDENCE:
            continue
        x, y, w, h = obs[2]
        left, right = max(0.0, x - RETRY_PAD), min(1.0, x + w + RETRY_PAD)
        bottom, top = max(0.0, y - RETRY_PAD), min(1.0, y + h + RETRY_PAD)
        crop = image.crop(
            (
                int(left * image.width),
                int((1 - top) * image.height),
                int(right * image.width),
                int((1 - bottom) * image.height),
            )
        )
        retried = []
        for text, conf, (cx, cy, cw, ch) in recognize(crop):
            box = (
                left + cx * (right - left),
                bottom + cy * (top - bottom),
                cw * (right - left),
                ch * (top - bottom),
            )
            if not any(overlap(box, other[2]) > 0.5 for other in kept):
                retried.append((text, conf, box))
        # Nothing new from the crop: the original reading is still better than a gap.
        kept.extend(retried or [obs])
    retries = len(results) - sum(obs[1] >= LOW_CONFIDENCE for obs in results)
    return group_lines(kept), retries


def draw_overlay(pdf: canvas.Canvas, lines, width: float, height: float) -> int:
    """Draw one invisible-text page; return its non-whitespace character count."""
    pdf.setPageSize((width, height))
    chars = 0
    for line in lines:
        text = "  ".join(run[4] for run in line)
        left = line[0][0] * width
        right = max(run[0] + run[2] for run in line) * width
        bottom = min(run[1] for run in line) * height
        top = max(run[1] + run[3] for run in line) * height
        size = max((top - bottom) * 0.8, 1.0)

        obj = pdf.beginText()
        obj.setTextRenderMode(3)
        obj.setFont(FONT_NAME, size)
        natural = pdfmetrics.stringWidth(text, FONT_NAME, size)
        if natural > 0 and right > left:
            # Stretch to the recognized box so selection lines up with the image.
            obj.setHorizScale(100 * (right - left) / natural)
        # Glyph descenders sit below the baseline, roughly a fifth of the box.
        obj.setTextOrigin(left, bottom + (top - bottom) * 0.2)
        obj.textOut(text)
        pdf.drawText(obj)
        chars += nonspace(text)
    pdf.showPage()
    return chars


def displayed_size(page) -> tuple[float, float]:
    box = page.cropbox
    if page.rotation % 180:
        return float(box.height), float(box.width)
    return float(box.width), float(box.height)


def displayed_to_user(page) -> tuple[float, ...]:
    """Matrix taking displayed-frame coordinates (origin at the displayed
    bottom-left of the crop box) into the page's unrotated user space."""
    box = page.cropbox
    x0, y0, x1, y1 = (float(v) for v in (box.left, box.bottom, box.right, box.top))
    return {
        0: (1, 0, 0, 1, x0, y0),
        90: (0, 1, -1, 0, x1, y0),
        180: (-1, 0, 0, -1, x1, y1),
        270: (0, -1, 1, 0, x0, y1),
    }[page.rotation % 360]


def page_resources(page) -> DictionaryObject:
    """The page's resources, inherited from /Parent if need be, as a new dict
    owned by this page alone. Scans often share one resource dict across every
    page, so adding to it in place would leak this page's layer into the rest."""
    node = page
    while node is not None and "/Resources" not in node:
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    source = node["/Resources"].get_object() if node is not None else DictionaryObject()
    return DictionaryObject(source)


def add_stream(writer: PdfWriter, data: bytes, entries: dict | None = None):
    stream = DecodedStreamObject()
    stream.set_data(data)
    if entries:
        stream.update(entries)
    # _add_object is private, but it is the only way to register a new stream
    # with a writer, and it has been stable across pypdf releases.
    return writer._add_object(stream.flate_encode())


def graft(writer: PdfWriter, page, layer) -> None:
    """Append LAYER (an overlay page in the displayed frame) to PAGE as a Form
    XObject, the way ocrmypdf grafts its text layer.

    The original content streams are kept by reference, byte for byte. Merging
    with pypdf's merge_page would instead parse and rewrite them, which is slow
    and, for "scans" that are really outlined vector glyphs with megabytes of
    path operators per page, inflates the file several-fold.
    """
    width, height = displayed_size(page)
    form = add_stream(
        writer,
        layer.get_contents().get_data(),
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(
                [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
            ),
            NameObject("/Resources"): layer["/Resources"].clone(writer),
        },
    )

    resources = page_resources(page)
    xobjects = DictionaryObject(resources.get("/XObject", DictionaryObject()).get_object())
    name = "/OCRText"
    while name in xobjects:
        name += "X"
    xobjects[NameObject(name)] = form
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources

    contents = page.get("/Contents")
    original = []
    if contents is not None:
        resolved = contents.get_object()
        original = list(resolved) if isinstance(resolved, ArrayObject) else [contents]
    # Isolate the original graphics state so an unbalanced q/Q or a leftover
    # cm in a scanner's content stream cannot displace the text layer.
    matrix = " ".join(f"{value:g}" for value in displayed_to_user(page))
    head = add_stream(writer, b"q\n")
    tail = add_stream(writer, f"\nQ\nq {matrix} cm {name} Do Q\n".encode())
    page[NameObject("/Contents")] = ArrayObject([head, *original, tail])


def main() -> int:
    args = parse_args()
    if args.min_chars_per_page < 0 or args.dpi <= 0:
        print("error: --min-chars-per-page must be non-negative and --dpi positive", file=sys.stderr)
        return EXIT_ERROR
    if not args.pdf.is_file():
        print(f"error: PDF not found: {args.pdf}", file=sys.stderr)
        return EXIT_ERROR
    if not FONT_PATH.is_file():
        print(f"error: overlay font not found: {FONT_PATH}", file=sys.stderr)
        return EXIT_ERROR

    started = time.monotonic()
    try:
        writer = PdfWriter(clone_from=PdfReader(args.pdf))
        rendered = pdfium.PdfDocument(args.pdf)
        page_count = len(writer.pages)
        if page_count == 0 or len(rendered) != page_count:
            raise RuntimeError(f"page count mismatch: pypdf {page_count}, pdfium {len(rendered)}")
    except Exception as exc:
        print(f"error: cannot open PDF {args.pdf}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    temp_path: Path | None = None
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))

        # Pass 1: decide and recognize. Overlays are drawn afterwards into a single
        # reportlab document so every OCR'd page shares one embedded font subset
        # instead of each page carrying its own copy.
        total_chars = 0
        retries = 0
        todo: list[tuple[int, list]] = []
        for index, page in enumerate(writer.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            chars = nonspace(text)
            if args.all_pages or chars < args.min_chars_per_page:
                lines, page_retries = recognize_page(
                    rendered[index], args.level, args.lang, args.dpi
                )
                retries += page_retries
                if lines:
                    todo.append((index, lines))
            total_chars += chars

        # Pass 2: draw each overlay in the page's displayed frame (pdfium renders
        # the crop box with /Rotate applied), then graft it onto the page.
        buffer = io.BytesIO()
        overlay = canvas.Canvas(buffer, pageCompression=1)
        for index, lines in todo:
            width, height = displayed_size(writer.pages[index])
            total_chars += draw_overlay(overlay, lines, width, height)
        if todo:
            overlay.save()
            for (index, _lines), layer in zip(todo, PdfReader(buffer).pages):
                graft(writer, writer.pages[index], layer)

        minimum_chars = page_count * args.min_chars_per_page
        if total_chars < minimum_chars:
            print(
                "unsupported scan: too little text even after OCR "
                f"({total_chars} non-whitespace characters across {page_count} pages; "
                f"minimum {minimum_chars}): {args.pdf}",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED_SCAN

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{args.output.name}.",
            suffix=".tmp",
            dir=args.output.parent,
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            writer.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, args.output)
        temp_path = None

        print(
            f"ocr: {len(todo)} of {page_count} pages recognized in "
            f"{time.monotonic() - started:.0f}s ({total_chars} non-whitespace characters, "
            f"{retries} low-confidence regions retried) "
            f"-> {args.output}"
        )
        return 0
    except Exception as exc:
        print(f"error: failed to OCR {args.pdf}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        rendered.close()


if __name__ == "__main__":
    raise SystemExit(main())
