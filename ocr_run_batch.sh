#!/usr/bin/env bash
#
# OCR-based batch: give scanned protocol PDFs an invisible OCR text layer, then
# hand the searchable copies to the unchanged run_batch.sh.
#
#   ./ocr_run_batch.sh --pdf-list need_ocr.csv --ocr-only    # the OCR step alone
#   ./ocr_run_batch.sh --pdf-list need_ocr.csv --dry-run     # OCR, then list jobs
#   ./ocr_run_batch.sh --pdf-list need_ocr.csv --workers 3   # OCR, then write SAPs
#   ./ocr_run_batch.sh --input-dir ~/downloads/nejm          # find the scans in a dir
#
# Each copy lands at OCR_DIR/<journal>/<article-id>/<file>_protocol.pdf, mirroring
# the last three components of the source path, so run_batch.sh derives the same id
# the scan had in earlier ledgers. Pages keep their numbers, and pages that already
# had a text layer are copied untouched. The OCR step skips copies newer than their
# source, so re-running the same command only OCRs what is new.
#
# run_batch.sh then reads every PDF under OCR_DIR, so the OCR dir, not the list,
# is the work set; use a separate --ocr-dir to keep batches apart. Any option not
# listed below is passed through to run_batch.sh (--workers, --model, --resume,
# --dry-run, ...), and a --limit given here overrides the default of "all of them".

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PDF_LIST=""
INPUT_DIR=""
OCR_DIR="ocr_pdfs"
OUT_DIR="ocr_result"
OCR_WORKERS=4
OCR_LEVEL="accurate"
FORCE_OCR=0
OCR_ONLY=0
PASS=()

OCR_DEPS=(--with pypdf --with pypdfium2 --with ocrmac --with reportlab)

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options (everything else goes to run_batch.sh):
  --pdf-list FILE     PDFs to OCR: a CSV with a pdf_path column (need_ocr.csv), or
                      one path per line (blank lines and # comments ignored)
  --input-dir DIR     instead, find *_protocol*.pdf under DIR and OCR only those
                      tools/extract_pdf.py rejects as scans
  --ocr-dir DIR       searchable copies, and run_batch.sh's input (default: ocr_pdfs)
  --out-dir DIR       run_batch.sh output; OCR logs go to DIR/logs (default: ocr_result)
  --ocr-workers N     PDFs OCR'd in parallel (default: 4)
  --ocr-level LEVEL   Apple Vision recognition level: accurate or fast (default: accurate)
  --force-ocr         OCR again even if an up-to-date copy exists
  --ocr-only          stop after the OCR step
  -h, --help          show this message
EOF
}

# ocr_one SRC DEST ID -- one PDF, run by the xargs workers below.
# Appends "ID<TAB>STATE" to $OCR_STATUS so the parent can summarize; the row is
# far below PIPE_BUF, so parallel appends do not interleave.
ocr_one() {
    local src="$1" dest="$2" id="$3"
    local log="$OCR_LOG_DIR/${id}.ocr.log" probe rc summary

    if [[ -s "$dest" && "$dest" -nt "$src" && "$FORCE_OCR" != "1" ]]; then
        printf '%s\tSKIP\n' "$id" >>"$OCR_STATUS"
        echo "[skip] $id (OCR copy up to date)"
        return 0
    fi

    : >"$log"
    printf 'Source: %s\nOCR copy: %s\n\n' "$src" "$dest" >>"$log"

    # With --input-dir the selection is every protocol PDF, so the scans have to
    # be told apart first, by the same check run_batch.sh will apply.
    if [[ "$OCR_PROBE" == "1" ]]; then
        probe="$(mktemp "${TMPDIR:-/tmp}/ocr_probe.XXXXXX")"
        "$OCR_UV" run --with pypdf tools/extract_pdf.py "$src" "$probe" >>"$log" 2>&1
        rc=$?
        rm -f "$probe"
        if [[ $rc -eq 0 ]]; then
            printf '%s\tTEXT\n' "$id" >>"$OCR_STATUS"
            echo "[text] $id has a text layer; not OCR'd"
            return 0
        elif [[ $rc -ne 3 ]]; then
            printf '%s\tFAIL\n' "$id" >>"$OCR_STATUS"
            echo "[ocr-FAIL] $id text probe failed -> $log" >&2
            return 1
        fi
    fi

    mkdir -p "$(dirname "$dest")"
    "$OCR_UV" run "${OCR_DEPS[@]}" tools/ocr_pdf.py --level "$OCR_LEVEL" "$src" "$dest" >>"$log" 2>&1
    rc=$?
    if [[ $rc -eq 0 ]]; then
        summary="$(grep -m1 '^ocr: ' "$log" | sed -E 's/^ocr: ([0-9]+) of ([0-9]+) pages recognized in ([0-9]+s).*/\1\/\2 pages \3/')"
        printf '%s\tOK\n' "$id" >>"$OCR_STATUS"
        echo "[ocr]  $id  $summary"
    elif [[ $rc -eq 3 ]]; then
        printf '%s\tSCAN\n' "$id" >>"$OCR_STATUS"
        echo "[scan] $id too little text even after OCR -> $log"
    else
        printf '%s\tFAIL\n' "$id" >>"$OCR_STATUS"
        echo "[ocr-FAIL] $id -> $log" >&2
        return 1
    fi
    return 0
}

if [[ "${1:-}" == "--ocr-one" ]]; then
    [[ $# -eq 4 ]] || { echo "--ocr-one requires SRC DEST ID" >&2; exit 2; }
    cd "$REPO_DIR" || exit 1
    ocr_one "$2" "$3" "$4"
    exit $?
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pdf-list)    [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; PDF_LIST="$2"; shift 2 ;;
        --input-dir)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; INPUT_DIR="$2"; shift 2 ;;
        --ocr-dir)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OCR_DIR="$2"; shift 2 ;;
        --out-dir)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OUT_DIR="$2"; shift 2 ;;
        --ocr-workers) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OCR_WORKERS="$2"; shift 2 ;;
        --ocr-level)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OCR_LEVEL="$2"; shift 2 ;;
        --force-ocr)   FORCE_OCR=1; shift ;;
        --ocr-only)    OCR_ONLY=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             PASS+=("$1"); shift ;;
    esac
done

[[ -n "$PDF_LIST" || -n "$INPUT_DIR" ]] || { echo "give --pdf-list FILE or --input-dir DIR" >&2; usage >&2; exit 2; }
[[ -z "$PDF_LIST" || -z "$INPUT_DIR" ]] || { echo "--pdf-list and --input-dir are mutually exclusive" >&2; exit 2; }
[[ -z "$PDF_LIST" || -r "$PDF_LIST" ]] || { echo "pdf list not readable: $PDF_LIST" >&2; exit 1; }
[[ -z "$INPUT_DIR" || -d "$INPUT_DIR" ]] || { echo "input dir not found: $INPUT_DIR" >&2; exit 1; }
[[ "$OCR_WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo "ocr-workers must be a positive integer" >&2; exit 2; }
case "$OCR_LEVEL" in
    accurate|fast) ;;
    *) echo "ocr-level must be one of: accurate, fast" >&2; exit 2 ;;
esac
[[ "$(uname -s)" == "Darwin" ]] || { echo "OCR uses Apple Vision (ocrmac) and needs macOS" >&2; exit 1; }
UV_BIN="$(command -v uv || true)"
[[ -n "$UV_BIN" ]] || { echo "uv executable not found" >&2; exit 1; }

# Collect sources while still in the caller's directory, so relative paths in the
# list, or a relative --input-dir, mean what the caller meant.
SOURCES="$(mktemp "${TMPDIR:-/tmp}/ocr_sources.XXXXXX")"
OCR_STATUS="$(mktemp "${TMPDIR:-/tmp}/ocr_status.XXXXXX")"
trap 'rm -f "$SOURCES" "$SOURCES.jobs" "$OCR_STATUS"' EXIT
if [[ -n "$PDF_LIST" ]]; then
    # A plain CSV reader: need_ocr.csv has no quoted fields, and paths hold no commas.
    awk -F, -v cwd="$PWD" '
        { gsub(/\r/, "") }
        NR == 1 { for (i = 1; i <= NF; i++) if ($i == "pdf_path") col = i; if (col) next }
        {
            path = col ? $col : $0
            if (!col && (path ~ /^[[:space:]]*$/ || path ~ /^[[:space:]]*#/)) next
            if (path !~ /^\//) path = cwd "/" path
            print path
        }
    ' "$PDF_LIST" >"$SOURCES"
    OCR_PROBE=0
else
    INPUT_DIR="$(cd "$INPUT_DIR" && pwd)"
    find "$INPUT_DIR" -type f -name '*_protocol*.pdf' -print | sort >"$SOURCES"
    OCR_PROBE=1
fi

cd "$REPO_DIR" || exit 1
[[ -f tools/ocr_pdf.py && -f tools/extract_pdf.py && -x run_batch.sh ]] \
    || { echo "tools/ocr_pdf.py, tools/extract_pdf.py, or run_batch.sh missing in $REPO_DIR" >&2; exit 1; }
[[ "$OCR_DIR" == /* ]] || OCR_DIR="$REPO_DIR/${OCR_DIR#./}"
[[ "$OUT_DIR" == /* ]] || OUT_DIR="$REPO_DIR/${OUT_DIR#./}"
mkdir -p "$OCR_DIR" "$OUT_DIR/logs"

# SRC -> DEST and id. DEST mirrors <journal>/<article-id>/<file>, adding _protocol
# to a stem that lacks it: run_batch.sh only finds *_protocol*.pdf, and the JAMA
# supplements in need_ocr.csv are named without it. The id is what run_batch.sh's
# derive_id will make of DEST.
awk -F/ -v root="$OCR_DIR" '
    {
        if (NF < 4) { printf "warn: path too shallow for <journal>/<article>/<file>, skipped: %s\n", $0 > "/dev/stderr"; next }
        stem = $NF; sub(/\.[Pp][Dd][Ff]$/, "", stem)
        if (stem !~ /_protocol/) stem = stem "_protocol"
        dest = root "/" $(NF - 2) "/" $(NF - 1) "/" stem ".pdf"
        id = $(NF - 2) "++" $(NF - 1) "++" stem
        gsub(/:/, "_", id)
        if (dest in seen) { printf "warn: %s and %s map to the same copy, skipped the second\n", seen[dest], $0 > "/dev/stderr"; next }
        seen[dest] = $0
        printf "%s\t%s\t%s\n", $0, dest, id
    }
' "$SOURCES" | while IFS=$'\t' read -r src dest id; do
    if [[ -f "$src" ]]; then
        printf '%s\0%s\0%s\0' "$src" "$dest" "$id"
    else
        echo "warn: not found, skipped: $src" >&2
    fi
done >"$SOURCES.jobs"

SELECTED="$(tr -cd '\0' <"$SOURCES.jobs" | wc -c | tr -d ' ')"
SELECTED=$((SELECTED / 3))
[[ "$SELECTED" -gt 0 ]] || { echo "no PDFs selected" >&2; exit 1; }

SCOPE="listed"
[[ "$OCR_PROBE" == "1" ]] && SCOPE="scans only"
echo "ocr     : $SELECTED pdf(s), $SCOPE -> $OCR_DIR  (workers=$OCR_WORKERS level=$OCR_LEVEL)"
echo "ocr logs: $OUT_DIR/logs/<id>.ocr.log"

# One-time dependency check, which also warms the uv cache so the workers do not
# each resolve the environment at once.
"$UV_BIN" run "${OCR_DEPS[@]}" python -c 'import ocrmac, pypdfium2, reportlab, pypdf' \
    || { echo "OCR dependencies failed to load (uv run ${OCR_DEPS[*]})" >&2; exit 1; }

export OCR_UV="$UV_BIN" OCR_LEVEL OCR_PROBE FORCE_OCR OCR_STATUS OCR_LOG_DIR="$OUT_DIR/logs"
START=$SECONDS
xargs -0 -n 3 -P "$OCR_WORKERS" "$REPO_DIR/ocr_run_batch.sh" --ocr-one <"$SOURCES.jobs"
ELAPSED=$((SECONDS - START))

count() { awk -F'\t' -v s="$1" '$2 == s' "$OCR_STATUS" | wc -l | tr -d ' '; }
FAILED_IDS="$(awk -F'\t' '$2 == "FAIL" { print $1 }' "$OCR_STATUS" | tr '\n' ' ')"
echo
echo "ocr done: $(count OK) ocr'd, $(count SKIP) up to date, $(count TEXT) had text, $(count SCAN) unreadable, $(count FAIL) failed (${ELAPSED}s)"
[[ -z "$FAILED_IDS" ]] || echo "ocr failed: $FAILED_IDS"

[[ "$OCR_ONLY" -eq 1 ]] && { [[ -z "$FAILED_IDS" ]]; exit $?; }

AVAILABLE="$(find "$OCR_DIR" -type f -name '*_protocol*.pdf' | wc -l | tr -d ' ')"
[[ "$AVAILABLE" -gt 0 ]] || { echo "no searchable PDFs in $OCR_DIR to process" >&2; exit 1; }
echo
# exec does not run the EXIT trap, so clean up by hand first.
rm -f "$SOURCES" "$SOURCES.jobs" "$OCR_STATUS"
trap - EXIT
exec "$REPO_DIR/run_batch.sh" --input-dir "$OCR_DIR" --out-dir "$OUT_DIR" --limit "$AVAILABLE" ${PASS[@]+"${PASS[@]}"}
