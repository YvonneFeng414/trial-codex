"""PDF -> pages.jsonl via the generator's own extractor (D5), with scanned- and TOC-page flags.

Each record keeps the extractor's text verbatim in `text`, so a citation's page N is the
page index by construction. `clean` is the same text with the protocol's margin line
numbers removed: many protocols number every line, and those numbers would otherwise
match nearly any number in a fact and hide NUM_MISS.
"""

from __future__ import annotations

import json
import re
import unicodedata
import shutil
import subprocess
import sys
from pathlib import Path

from qc import TOOLS_DIR
from qc.text import sha

MIN_CHARS = 20
# Share of control characters above which a page's text layer is glyph codes, not text: a
# font without a ToUnicode map extracts "Statistical" as "6WDWLVWLFDO" with \x03 for spaces.
# Clean protocols peak near 1.3%; garbled pages run 35-74% (jama.2022.9265, jama.2024.9779).
GARBLED_SHARE = 0.02
PAGE_MARKER_RE = re.compile(r"^===== PAGE (\d+) OF (\d+) =====$", re.MULTILINE)
# The plan's rule is `\.{5,}\s*\d+$`; the optional second number admits a margin line
# number after the page number ("1.1 DISEASE BACKGROUND ........ 10 51"), which the
# plan's pattern misses on line-numbered protocols such as LACE.
TOC_LINE_RE = re.compile(r"\.{5,}\s*\d+(?:\s+\d+)?\s*$")
TOC_SHARE = 0.30
OCR_DEPS = ("--with", "pypdf", "--with", "pypdfium2", "--with", "ocrmac", "--with", "reportlab")
PAGE_OCR_SCRIPT = Path(__file__).with_name("ocr_pages.py")
_TRAILING_INT_RE = re.compile(r"(\d{1,5})\s*$")


def split_pages(extracted: str) -> list[tuple[int, str]]:
    markers = list(PAGE_MARKER_RE.finditer(extracted))
    pages: list[tuple[int, str]] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(extracted)
        pages.append((int(marker.group(1)), extracted[marker.end() : end].strip("\n")))
    return pages


def nonspace(text: str) -> int:
    return sum(not char.isspace() for char in text)


def control_share(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    return sum(unicodedata.category(c) == "Cc" for c in visible) / max(len(visible), 1)


def is_toc(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return sum(bool(TOC_LINE_RE.search(line)) for line in lines) / len(lines) >= TOC_SHARE


def strip_line_numbers(text: str, min_lines: int = 5, min_share: float = 0.4, strong_run: int = 8) -> str:
    """Remove a margin line-number sequence from line ends, if the page has one.

    A trailing integer is a line number when it continues an increasing run (step 1-3)
    with its neighbours. The number may be glued to the text ("2.1.21701", "non-1691"),
    so once a run is established the expected next value is also accepted as a suffix.
    """
    lines = text.split("\n")
    trailing: dict[int, int] = {}
    for index, line in enumerate(lines):
        match = _TRAILING_INT_RE.search(line)
        if match:
            trailing[index] = int(match.group(1))
    indices = sorted(trailing)
    in_run: set[int] = set()
    for previous, current in zip(indices, indices[1:]):
        step = trailing[current] - trailing[previous]
        if 1 <= step <= 3 * (current - previous):
            in_run.update((previous, current))
    nonempty = sum(bool(line.strip()) for line in lines)
    # A long run is enough on its own: table-heavy pages have many unnumbered cell lines,
    # which would otherwise push the numbered share under min_share (LACE p78, p92).
    if len(in_run) < min_lines or (len(in_run) < strong_run and len(in_run) < min_share * max(nonempty, 1)):
        return text

    out: list[str] = []
    expected: int | None = None
    for index, line in enumerate(lines):
        if index in in_run:
            digits = _TRAILING_INT_RE.search(line).group(1)
            if expected is not None and digits != str(expected) and digits.endswith(str(expected)):
                digits = str(expected)  # glued: strip only the line-number suffix
            line = line[: line.rstrip().rfind(digits)].rstrip() if digits else line
            expected = int(digits) + 1
        elif expected is not None:
            match = re.search(rf"{expected}\s*$", line)
            if match and line.strip() != str(expected):
                line = line[: match.start()].rstrip()
                expected += 1
            elif line.strip() == str(expected):
                line = ""
                expected += 1
        out.append(line)
    return "\n".join(out)


def _extract(pdf: Path, txt: Path) -> None:
    command = [sys.executable, str(TOOLS_DIR / "extract_pdf.py"), str(pdf), str(txt)]
    # Thin pages are flagged per page below, so the whole-document scan gate is off here.
    result = subprocess.run(command + ["--min-chars-per-page", "0"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"extract_pdf.py failed on {pdf}: {result.stderr.strip()}")


def _ocr(pdf: Path, out_pdf: Path, log: list[str]) -> bool:
    """Send the PDF through tools/ocr_pdf.py, the UNSUPPORTED_SCAN path (macOS only)."""
    uv = shutil.which("uv")
    if uv is None or sys.platform != "darwin":
        log.append("ocr skipped: needs uv and macOS (Apple Vision)")
        return False
    command = [uv, "run", *OCR_DEPS, str(TOOLS_DIR / "ocr_pdf.py"), str(pdf), str(out_pdf)]
    command += ["--min-chars-per-page", str(MIN_CHARS)]
    result = subprocess.run(command, capture_output=True, text=True)
    # Exit 3 still writes nothing; any other failure leaves the original text in place.
    if result.returncode != 0 or not out_pdf.is_file():
        log.append(f"ocr failed (exit {result.returncode}): {result.stderr.strip()[-300:]}")
        return False
    return True


def _ocr_page_images(pdf: Path, numbers: list[int], log: list[str]) -> dict[int, str]:
    """OCR pages from their rendered image (qc/ocr_pages.py), for garbled text layers."""
    uv = shutil.which("uv")
    if uv is None or sys.platform != "darwin":
        log.append("garbled-page ocr skipped: needs uv and macOS (Apple Vision)")
        return {}
    command = [uv, "run", "--with", "pypdfium2", "--with", "ocrmac", str(PAGE_OCR_SCRIPT), str(pdf), *map(str, numbers)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        log.append(f"garbled-page ocr failed (exit {result.returncode}): {result.stderr.strip()[-300:]}")
        return {}
    return {int(k): v for k, v in json.loads(result.stdout).items()}


def build(pdf: Path, workdir: Path, stem: str, ocr: bool = True, ocr_garbled: bool = True) -> tuple[list[dict], dict]:
    """Extract, OCR thin pages if asked and garbled pages by default; return the page records.

    A garbled page (its text layer is glyph codes) gets the OCR text of its rendered image
    instead, when that text is itself clean; the page number is unchanged.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    txt = workdir / f"{stem}.extracted.txt"
    log: list[str] = []
    _extract(pdf, txt)
    raw = split_pages(txt.read_text(encoding="utf-8"))
    ocr_pages: set[int] = set()

    thin = [number for number, text in raw if nonspace(text) < MIN_CHARS]
    if thin and ocr:
        ocr_pdf = workdir / f"{stem}.ocr.pdf"
        if _ocr(pdf, ocr_pdf, log):
            ocr_txt = workdir / f"{stem}.ocr.extracted.txt"
            _extract(ocr_pdf, ocr_txt)
            redone = dict(split_pages(ocr_txt.read_text(encoding="utf-8")))
            if len(redone) == len(raw):
                for number in thin:
                    if nonspace(redone[number]) > nonspace(dict(raw)[number]):
                        ocr_pages.add(number)
                raw = [(number, redone[number] if number in ocr_pages else text) for number, text in raw]
            else:
                log.append("ocr changed the page count; OCR text discarded")

    garbled_before = [n for n, text in raw if nonspace(text) >= MIN_CHARS and control_share(text) > GARBLED_SHARE]
    if garbled_before and ocr_garbled:
        recognized = _ocr_page_images(pdf, garbled_before, log)
        replaced = {n for n, text in recognized.items()
                    if nonspace(text) >= MIN_CHARS and control_share(text) <= GARBLED_SHARE}
        ocr_pages |= replaced
        raw = [(n, recognized[n] if n in replaced else text) for n, text in raw]
        log.append(f"garbled pages: {len(garbled_before)}, replaced by image OCR: {len(replaced)}")

    records = []
    for number, text in raw:
        chars = nonspace(text)
        garbled = chars >= MIN_CHARS and control_share(text) > GARBLED_SHARE
        records.append(
            {
                "page": number,
                "text": text,
                "clean": strip_line_numbers(text),
                "n_chars": chars,
                "garbled": garbled,
                "unsearchable": chars < MIN_CHARS or garbled,
                "toc": is_toc(text),
                "ocr": number in ocr_pages,
            }
        )
    meta = {
        "pdf": str(pdf),
        "pdf_sha": sha(pdf.read_bytes(), 32),
        "n_pages": len(records),
        "thin_before_ocr": thin,
        "garbled_before_ocr": garbled_before,
        "ocr_garbled": ocr_garbled,
        "ocr_pages": sorted(ocr_pages),
        "unsearchable": [r["page"] for r in records if r["unsearchable"]],
        "garbled": [r["page"] for r in records if r["garbled"]],
        "toc": [r["page"] for r in records if r["toc"]],
        "log": log,
    }
    return records, meta


def load(pdf: Path, workdir: Path, stem: str, ocr: bool = True, force: bool = False, ocr_garbled: bool = True):
    """Cached build: reuse <stem>.pages.jsonl while the PDF's hash is unchanged."""
    pages_path = workdir / f"{stem}.pages.jsonl"
    meta_path = workdir / f"{stem}.pages.meta.json"
    if not force and pages_path.is_file() and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta.get("pdf_sha") == sha(pdf.read_bytes(), 32) and "garbled" in meta
                and meta.get("ocr_garbled") == ocr_garbled and (meta.get("ocr") == ocr or not meta.get("thin_before_ocr"))):
            with open(pages_path, encoding="utf-8") as stream:
                return [json.loads(line) for line in stream], meta
    records, meta = build(pdf, workdir, stem, ocr=ocr, ocr_garbled=ocr_garbled)
    meta["ocr"] = ocr
    with open(pages_path, "w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return records, meta


def searchable(pages: list[dict]) -> list[dict]:
    """Pages retrieval and absence search may use: not TOC, not unsearchable."""
    return [page for page in pages if not page["toc"] and not page["unsearchable"]]
