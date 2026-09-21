#!/usr/bin/env bash
#
# Batch-run protocol PDF -> SAP extraction with one Codex session per PDF.
#
#   ./run_batch.sh --dry-run          # build/print a two-file manifest only
#   ./run_batch.sh --limit 2          # smoke test on two protocols
#   ./run_batch.sh --limit 50         # process 50 protocols with two workers
#   ./run_batch.sh --offset 50        # continue with protocols 51-100
#   ./run_batch.sh --progress verbose # mirror each Codex session to the terminal
#   ./run_batch.sh --notify-test      # check the banner and email path, then exit
#
#   ./run_batch.sh --session-budget 4h --limit 200   # stop cleanly before the limit
#   ./run_batch.sh --resume --ignore-stop            # pick up after a drain
#   tools/budget.sh --probe                          # quota left, costs no tokens
#
# Re-running the same command skips completed outputs and retries unfinished jobs.
# A finished run raises a local banner and, if RB_SMTP_* is configured, emails a
# one-line summary so a multi-hour batch does not need watching.
#
# Jobs are admitted one PDF at a time and only while enough budget remains to
# finish one, so a session limit stops the run at a clean boundary rather than
# mid-edit. Progress lives in _ledger.tsv, which is never truncated. Exit codes:
# 0 done, 1 some job failed, 3 drained on budget with work left, 4 refused to
# start because a previous drain has not been cleared.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Isolate Codex credentials, configuration, sessions, and logs to this project.
# The directory is gitignored because it contains auth.json and other private state.
export CODEX_HOME="$REPO_DIR/.codex-home"

INPUT_DIR="/Users/yixuanfeng/Desktop/web-download/downloads/nejm" # TODO: change this input dir
OUT_DIR="test_result"
LIMIT=3
OFFSET=0
WORKERS=3
RETRIES=1
RETRY_SLEEP=15
MODEL="gpt-5.6-terra"
REVIEW_MODEL="gpt-5.6-luna"
VERBOSITY="high"
PROGRESS="normal"
FORCE=0
DRY_RUN=0
NOTIFY_TEST=0
SESSION_BUDGET=""
SESSION_TOKENS=0
SOFT_PCT=80
HARD_PCT=92
HEADROOM_FACTOR=1.0
PROBE=auto
KEEP_JSONL=on-error
STOP_FILE=""
IGNORE_STOP=0
RESUME=0

usage() {
    sed -n '3,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --input-dir DIR   root searched recursively for *_protocol*.pdf
  --out-dir DIR     output directory (default: test_result)
  --limit N         number of PDFs to process (default: 3)
  --offset N        skip the first N PDFs (default: 0)
  --workers N       parallel primary Codex sessions (default: 3; each uses a reviewer)
  --retries N       retries after an agent/lint failure (default: 1)
  --model NAME      model passed to `codex exec` (default: gpt-5.6-terra)
  --review-model N  reviewer subagent model (default: gpt-5.6-luna)
  --verbosity LEVEL SAP detail: low, medium, or high (default: high)
  --progress LEVEL  console detail: quiet, normal, or verbose (default: normal)
                    quiet   only the final summary on stdout; failures on stderr
                    normal  run header, one line per job, final summary
                    verbose adds per-step lines and mirrors extraction, the Codex
                            session, and the linter to the terminal, id-prefixed
  --force           re-run even if a final output exists
  --session-budget DUR  stop admitting jobs after DUR of wall clock (90m, 4h, 5400s)
  --headroom-factor F   start a job only if p90*F still fits the budget (default 1.0)
  --soft-limit-percent N  drain at/above N% of a rate-limit window (default 80)
  --hard-limit-percent N  refuse admission at/above N% (default 92)
  --session-token-budget N  stop admitting once spent+p90 exceeds N tokens
  --probe-rate-limits W auto|on|off; the zero-token account query (default auto)
  --keep-jsonl WHEN     always|on-error|never retain logs/<id>.jsonl (default on-error)
  --stop-file PATH      drain sentinel (default: OUT_DIR/.stop)
  --ignore-stop         clear an existing stop-file and start anyway
  --resume              take the work set from the ledger, not just the filesystem
  --dry-run         build the manifest and print jobs; run no extraction or agent
  --no-notify       suppress the local macOS banner
  --mail-to ADDR    email the run summary to ADDR (overrides $RB_MAIL_TO)
  --no-mail         suppress the email even if $RB_MAIL_TO is set
  --notify-test     send both notifications with a dummy summary and exit
  -h, --help        show this message

EOF
    rb_notify_usage
}

[[ -r "$REPO_DIR/tools/budget.sh" ]] || { echo "tools/budget.sh not found in $REPO_DIR" >&2; exit 1; }
. "$REPO_DIR/tools/budget.sh"

derive_id() {
    local stem id
    stem="$(basename "$1")"
    stem="${stem%.pdf}"
    id="$(printf '%s' "$stem" | grep -oE '^[a-zA-Z]+[0-9]+' || true)"
    printf '%s' "${id:-$stem}"
}

# progress LEVEL MESSAGE...
#   job   per-PDF outcome; hidden by --progress quiet
#   fail  per-PDF failure; goes to stderr under --progress quiet so stdout stays
#         limited to the final summary
#   step  intra-job detail; shown only by --progress verbose
progress() {
    local level="$1"
    shift
    case "$RB_PROGRESS" in
        quiet) [[ "$level" == "fail" ]] && echo "$@" >&2 ;;
        normal) [[ "$level" != "step" ]] && echo "$@" ;;
        verbose) echo "$@" ;;
    esac
    return 0
}

# run_step ID LOG COMMAND...
# Append the command's output to its job log, and under --progress verbose also mirror
# it to the terminal with an id prefix so parallel workers stay distinguishable.
# Returns the command's own status, never tee's.
run_step() {
    local id="$1" log="$2"
    shift 2

    if [[ "$RB_PROGRESS" != "verbose" ]]; then
        "$@" >>"$log" 2>&1
        return $?
    fi

    "$@" 2>&1 | tee -a "$log" | awk -v id="$id" '{ printf "[%s] %s\n", id, $0; fflush() }'
    return "${PIPESTATUS[0]}"
}

run_one() {
    local pdf="$1"
    local id final draft evidence review extracted log start elapsed bytes page_count
    local attempt rc lint_rc evidence_rc review_rc prompt jsonl lastmsg usage cls
    local in_tok=0 out_tok=0 tot_tok=0 admit_rc
    local -a codex_args

    # Drain gate. A tripped stop-file means finish what is running and start
    # nothing new; exiting 255 is what makes xargs stop dispatching the rest.
    if [[ -e "$RB_STOP" ]]; then
        id="$(derive_id "$pdf")"
        rb_append_ledger "$id" DEFERRED 0 0 0 0 0 0 "stop-file: $(rb_stop_reason)"
        progress step "[defer] $id (stop-file)"
        return 255
    fi

    id="$(derive_id "$pdf")"
    final="$RB_OUT_DIR/sap_${id}.md"
    draft="$RB_OUT_DIR/work/${id}.draft.md"
    evidence="$RB_OUT_DIR/work/${id}.evidence.md"
    review="$RB_OUT_DIR/work/${id}.review.md"
    extracted="$RB_OUT_DIR/work/${id}.protocol.txt"
    log="$RB_OUT_DIR/logs/${id}.log"
    jsonl="$RB_OUT_DIR/logs/${id}.jsonl"
    lastmsg="$RB_OUT_DIR/work/${id}.lastmsg.txt"
    RB_CURRENT_ID="$id"

    if [[ -s "$final" && "$RB_FORCE" != "1" ]]; then
        bytes="$(wc -c <"$final" | tr -d ' ')"
        rb_append_ledger "$id" SKIP 0 0 "$bytes" 0 0 0 "-"
        progress job "[skip] $id (output exists)"
        return 0
    fi

    # Admission. Asked before any work is done, so a job that cannot finish
    # inside the remaining budget is never started at all.
    rb_admit; admit_rc=$?
    if [[ "$admit_rc" -ne 0 ]]; then
        rb_trip_stop "$RB_ADMIT_REASON"
        rb_append_ledger "$id" DEFERRED 0 0 0 0 0 0 "$RB_ADMIT_REASON"
        progress job "[defer] $id  $RB_ADMIT_REASON"
        return 255
    fi

    start=$SECONDS
    : >"$log"
    printf 'PDF: %s\nExtracted text: %s\nEvidence: %s\nDraft: %s\nReview: %s\n\n' \
        "$pdf" "$extracted" "$evidence" "$draft" "$review" >>"$log"
    progress step "[start] $id  $(basename "$pdf")"

    run_step "$id" "$log" "$RB_UV" run --with pypdf tools/extract_pdf.py "$pdf" "$extracted"
    rc=$?
    if [[ $rc -eq 3 ]]; then
        elapsed=$((SECONDS - start))
        rb_append_ledger "$id" UNSUPPORTED_SCAN 0 "$elapsed" 0 0 0 0 "extract rc=3"
        progress job "[scan] $id requires OCR -> $log"
        return 0
    elif [[ $rc -ne 0 || ! -s "$extracted" ]]; then
        elapsed=$((SECONDS - start))
        rb_append_ledger "$id" FAIL 0 "$elapsed" 0 0 0 0 "extraction failed"
        progress fail "[FAIL] $id extraction failed -> $log"
        return 0
    fi

    page_count="$(awk 'NR == 1 { print $5; exit }' "$extracted")"
    if [[ ! "$page_count" =~ ^[1-9][0-9]*$ ]]; then
        elapsed=$((SECONDS - start))
        echo "error: could not determine page count from $extracted" >>"$log"
        rb_append_ledger "$id" FAIL 0 "$elapsed" 0 0 0 0 "invalid page count"
        progress fail "[FAIL] $id invalid extracted text -> $log"
        return 0
    fi
    progress step "[pages] $id  $page_count page(s) -> $extracted"

    prompt="Create or repair the SAP described in AGENTS.md.
Protocol text: $extracted
Protocol page count: $page_count
Output verbosity: $RB_VERBOSITY
Reviewer model: $RB_REVIEW_MODEL
Evidence output: $evidence
Draft output: $draft
Reviewer output: $review
Follow the evidence-first workflow in AGENTS.md. Spawn a reviewer subagent, resolve all
of its findings, and obtain an exact standalone VERDICT: PASS in the reviewer output.
Then run:
$RB_UV run tools/lint_sap.py $draft --max-page $page_count
Fix all violations before finishing."

    for ((attempt = 0; attempt <= RB_RETRIES; attempt++)); do
        if ((attempt > 0)); then
            progress job "[retry $attempt] $id"
            sleep "$RB_RETRY_SLEEP"
            printf '\n===== retry %d =====\n' "$attempt" >>"$log"
        fi

        # A verdict is valid only for the draft produced in this attempt. Evidence and
        # draft remain available to help a fresh retry, but review must be regenerated.
        : >"$review"

        codex_args=(
            exec
            -C "$REPO_DIR"
            --skip-git-repo-check
            --ephemeral
            --enable multi_agent
            --sandbox workspace-write
            --color never
            --json
            -o "$lastmsg"
        )
        if [[ -n "$RB_MODEL" ]]; then
            codex_args+=(--model "$RB_MODEL")
        fi

        progress step "[codex] $id  attempt $((attempt + 1))/$((RB_RETRIES + 1)) -> $log"
        printf '%s\n' "$prompt" | rb_run_codex "$id" "$log" "$jsonl" "$RB_CODEX" "${codex_args[@]}" -
        rc="${PIPESTATUS[1]}"

        usage="$(rb_scan_usage "$jsonl")"
        in_tok="$(printf '%s' "$usage" | cut -f1)"
        out_tok="$(printf '%s' "$usage" | cut -f2)"
        tot_tok="$(printf '%s' "$usage" | cut -f3)"

        # A quota stop is not a failure: it must not burn a retry, must not be
        # recorded as FAIL, and must drain every other worker.
        rb_classify_codex "$rc" "$jsonl" "$log"; cls=$?
        if [[ "$cls" -eq 2 ]]; then
            elapsed=$((SECONDS - start))
            rb_trip_stop "quota:$RB_QUOTA_VARIANT"
            rb_append_budget error 100 - - - "$tot_tok" "$RB_QUOTA_VARIANT"
            rb_append_ledger "$id" LIMIT_HIT "$((attempt + 1))" "$elapsed" 0 \
                "$in_tok" "$out_tok" "$tot_tok" "quota:$RB_QUOTA_VARIANT"
            rb_write_progress "$id" "quota:$RB_QUOTA_VARIANT"
            progress fail "[limit] $id  quota exhausted -> $log"
            return 255
        fi

        lint_rc=1
        evidence_rc=1
        review_rc=1
        if [[ -s "$evidence" ]]; then
            evidence_rc=0
        else
            echo "error: Codex did not produce evidence: $evidence" >>"$log"
        fi
        if [[ -s "$review" ]] \
            && grep -Fxq "Reviewer model: $RB_REVIEW_MODEL" "$review" \
            && grep -qx '## Final field checklist' "$review" \
            && grep -qx 'VERDICT: PASS' "$review"; then
            review_rc=0
        else
            echo "error: reviewer model declaration, final field checklist, or standalone VERDICT: PASS is missing: $review" >>"$log"
        fi
        if [[ -s "$draft" ]]; then
            run_step "$id" "$log" "$RB_UV" run tools/lint_sap.py "$draft" --max-page "$page_count"
            lint_rc=$?
        else
            echo "error: Codex did not produce draft: $draft" >>"$log"
        fi

        if [[ $rc -eq 0 && $evidence_rc -eq 0 && $review_rc -eq 0 && $lint_rc -eq 0 ]]; then
            mv -f "$draft" "$final"
            elapsed=$((SECONDS - start))
            bytes="$(wc -c <"$final" | tr -d ' ')"
            [[ "$RB_KEEP_JSONL" == "always" ]] || rm -f "$jsonl"
            rb_append_ledger "$id" OK "$((attempt + 1))" "$elapsed" "$bytes" \
                "$in_tok" "$out_tok" "$tot_tok" "-"
            progress job "[ok]   $id  ${elapsed}s  ${bytes}B"
            return 0
        fi
        progress step "[check] $id  codex=$rc evidence=$evidence_rc review=$review_rc lint=$lint_rc"
    done

    elapsed=$((SECONDS - start))
    [[ "$RB_KEEP_JSONL" == "never" ]] && rm -f "$jsonl"
    rb_append_ledger "$id" FAIL "$((RB_RETRIES + 1))" "$elapsed" 0 \
        "$in_tok" "$out_tok" "$tot_tok" "codex=$rc evidence=$evidence_rc review=$review_rc lint=$lint_rc"
    progress fail "[FAIL] $id  ${elapsed}s -> $log"
    return 0
}

if [[ "${1:-}" == "--run-one" ]]; then
    [[ $# -eq 2 ]] || { echo "--run-one requires one PDF path" >&2; exit 2; }
    cd "$REPO_DIR" || exit 1
    trap rb_on_signal INT TERM
    run_one "$2"
    exit $?
fi

# Notification sinks, and the credentials handling that comes with them. Sourced
# below the --run-one dispatch on purpose: a worker never notifies, so it should
# not read this file, and no credential path exists in the process that runs
# Codex. Defaults inside yield to anything already set, so the flags below win.
[[ -r "$REPO_DIR/tools/notify.sh" ]] || { echo "tools/notify.sh not found in $REPO_DIR" >&2; exit 1; }
. "$REPO_DIR/tools/notify.sh"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input-dir) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; INPUT_DIR="$2"; shift 2 ;;
        --out-dir)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OUT_DIR="$2"; shift 2 ;;
        --limit)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; LIMIT="$2"; shift 2 ;;
        --offset)    [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OFFSET="$2"; shift 2 ;;
        --workers)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; WORKERS="$2"; shift 2 ;;
        --retries)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; RETRIES="$2"; shift 2 ;;
        --model)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; MODEL="$2"; shift 2 ;;
        --review-model) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; REVIEW_MODEL="$2"; shift 2 ;;
        --verbosity) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; VERBOSITY="$2"; shift 2 ;;
        --progress)  [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; PROGRESS="$2"; shift 2 ;;
        --force)     FORCE=1; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --session-budget) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; SESSION_BUDGET="$2"; shift 2 ;;
        --headroom-factor) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; HEADROOM_FACTOR="$2"; shift 2 ;;
        --soft-limit-percent) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; SOFT_PCT="$2"; shift 2 ;;
        --hard-limit-percent) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; HARD_PCT="$2"; shift 2 ;;
        --session-token-budget) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; SESSION_TOKENS="$2"; shift 2 ;;
        --probe-rate-limits) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; PROBE="$2"; shift 2 ;;
        --keep-jsonl) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; KEEP_JSONL="$2"; shift 2 ;;
        --stop-file) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; STOP_FILE="$2"; shift 2 ;;
        --ignore-stop) IGNORE_STOP=1; shift ;;
        --resume)    RESUME=1; shift ;;
        --no-notify) RB_NOTIFY=0; shift ;;
        --mail-to)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; RB_MAIL_TO_FLAG="$2"; shift 2 ;;
        --no-mail)   RB_MAIL=0; shift ;;
        --notify-test) NOTIFY_TEST=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for value_name in LIMIT OFFSET WORKERS RETRIES; do
    value="${!value_name}"
    [[ "$value" =~ ^[0-9]+$ ]] || { echo "$(printf '%s' "$value_name" | tr '[:upper:]' '[:lower:]') must be a non-negative integer" >&2; exit 2; }
done
[[ "$LIMIT" -gt 0 ]] || { echo "limit must be greater than zero" >&2; exit 2; }
[[ "$WORKERS" -gt 0 ]] || { echo "workers must be greater than zero" >&2; exit 2; }
[[ -n "$MODEL" ]] || { echo "model must not be empty" >&2; exit 2; }
[[ -n "$REVIEW_MODEL" ]] || { echo "review model must not be empty" >&2; exit 2; }
case "$VERBOSITY" in
    low|medium|high) ;;
    *) echo "verbosity must be one of: low, medium, high" >&2; exit 2 ;;
esac
case "$PROGRESS" in
    quiet|normal|verbose) ;;
    *) echo "progress must be one of: quiet, normal, verbose" >&2; exit 2 ;;
esac
case "$PROBE" in
    auto|on|off) ;;
    *) echo "probe-rate-limits must be one of: auto, on, off" >&2; exit 2 ;;
esac
case "$KEEP_JSONL" in
    always|on-error|never) ;;
    *) echo "keep-jsonl must be one of: always, on-error, never" >&2; exit 2 ;;
esac
SESSION_BUDGET_S=0
if [[ -n "$SESSION_BUDGET" ]]; then
    SESSION_BUDGET_S="$(rb_parse_duration "$SESSION_BUDGET")" \
        || { echo "session-budget must look like 5400, 90m, 4h, or 1d" >&2; exit 2; }
fi
[[ "$SESSION_TOKENS" =~ ^[0-9]+$ ]] || { echo "session-token-budget must be a non-negative integer" >&2; exit 2; }
for pct_name in SOFT_PCT HARD_PCT; do
    pct_value="${!pct_name}"
    [[ "$pct_value" =~ ^[0-9]+$ && "$pct_value" -le 100 ]] \
        || { echo "$(printf '%s' "$pct_name" | tr '[:upper:]' '[:lower:]') must be an integer 0-100" >&2; exit 2; }
done
[[ "$SOFT_PCT" -lt "$HARD_PCT" ]] || { echo "soft-limit-percent must be below hard-limit-percent" >&2; exit 2; }
awk -v f="$HEADROOM_FACTOR" 'BEGIN { exit !(f + 0 > 0) }' \
    || { echo "headroom-factor must be a positive number" >&2; exit 2; }

cd "$REPO_DIR" || exit 1

if [[ "$OUT_DIR" != /* ]]; then
    OUT_DIR="$REPO_DIR/${OUT_DIR#./}"
fi
mkdir -p "$OUT_DIR/logs" "$OUT_DIR/work"
MANIFEST="$OUT_DIR/_manifest.tsv"
STATUS="$OUT_DIR/_status.tsv"
LEDGER="$OUT_DIR/_ledger.tsv"
BUDGET="$OUT_DIR/_budget.tsv"
STOP="${STOP_FILE:-$OUT_DIR/.stop}"

# Context tools/notify.sh reads when it composes a summary, plus its log
# destination, which belongs with this run's other output rather than in /tmp.
RB_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RB_NOTIFY_LOG="$OUT_DIR/logs/_notify.log"
RB_STATUS="$STATUS"
export RB_PROGRESS="$PROGRESS"

# --notify-test deliberately precedes the input-dir and executable checks: it
# proves the alert path on a machine that cannot run a batch at all.
if [[ "$NOTIFY_TEST" -eq 1 ]]; then
    rb_notify_test
    exit 0
fi

[[ -d "$INPUT_DIR" ]] || { echo "input dir not found: $INPUT_DIR" >&2; exit 1; }

CODEX_BIN="$(command -v codex || true)"
UV_BIN="$(command -v uv || true)"
[[ -n "$CODEX_BIN" ]] || { echo "codex executable not found" >&2; exit 1; }
[[ -n "$UV_BIN" ]] || { echo "uv executable not found" >&2; exit 1; }
JQ_BIN="$(command -v jq || true)"
[[ -n "$JQ_BIN" ]] || { echo "jq executable not found (needed to read codex --json output)" >&2; exit 1; }
[[ -f AGENTS.md ]] || { echo "AGENTS.md not found in $REPO_DIR" >&2; exit 1; }
[[ -f tools/extract_pdf.py ]] || { echo "tools/extract_pdf.py not found" >&2; exit 1; }
[[ -f tools/lint_sap.py ]] || { echo "tools/lint_sap.py not found" >&2; exit 1; }

# A stop-file left by the previous session means that run drained on budget.
# Starting again immediately would just re-trip it, so say so and stop.
touch "$LEDGER" "$BUDGET" 2>/dev/null
if [[ -e "$STOP" ]]; then
    if [[ "$IGNORE_STOP" -eq 1 ]]; then
        rm -f "$STOP"
    else
        echo "previous run drained: $(RB_STOP="$STOP" rb_stop_reason)" >&2
        echo "quota may not have reset yet. Check with: tools/budget.sh --probe" >&2
        echo "then re-run with --ignore-stop (or delete $STOP)" >&2
        exit 4
    fi
fi

: >"$MANIFEST"
while IFS= read -r pdf; do
    id="$(derive_id "$pdf")"
    printf '%s\t%s\t%s\n' "$id" "$pdf" "$OUT_DIR/sap_${id}.md"
done < <(
    find "$INPUT_DIR" -type f -name '*_protocol*.pdf' -print \
        | sort \
        | tail -n "+$((OFFSET + 1))" \
        | head -n "$LIMIT"
) >"$MANIFEST"

DEDUPED="$MANIFEST.dedup"
awk -F'\t' '{ if (seen[$1]++) print "warn: duplicate id " $1 ", skipping " $2 > "/dev/stderr"; else print }' \
    "$MANIFEST" >"$DEDUPED"
mv "$DEDUPED" "$MANIFEST"

# --resume takes the work set from the ledger as well as the filesystem. Its
# one real effect today: an id already proven to need OCR is never re-extracted,
# which otherwise happens on every single re-run forever.
if [[ "$RESUME" -eq 1 && -s "$LEDGER" ]]; then
    RESUMED="$MANIFEST.resume"
    awk -F'\t' -v led="$LEDGER" '
        BEGIN { while ((getline line < led) > 0) { split(line, f, "\t"); last[f[3]] = f[4] } }
        last[$1] != "UNSUPPORTED_SCAN" { print }
    ' "$MANIFEST" >"$RESUMED" && mv "$RESUMED" "$MANIFEST"
fi

TOTAL="$(wc -l <"$MANIFEST" | tr -d ' ')"
if [[ "$TOTAL" -eq 0 ]]; then
    echo "no *_protocol*.pdf found under $INPUT_DIR (offset=$OFFSET limit=$LIMIT)" >&2
    exit 1
fi

if [[ "$PROGRESS" != "quiet" ]]; then
    echo "input   : $INPUT_DIR"
    echo "output  : $OUT_DIR"
    echo "harness : $CODEX_BIN${MODEL:+  (model: $MODEL)}"
    echo "reviewer: $REVIEW_MODEL"
    echo "detail  : $VERBOSITY"
    echo "progress: $PROGRESS"
    echo "selected: $TOTAL pdf(s)  [offset $OFFSET, limit $LIMIT]  workers=$WORKERS retries=$RETRIES"
    echo
fi
if [[ "$PROGRESS" == "verbose" && "$WORKERS" -gt 1 ]]; then
    echo "note: $WORKERS workers stream concurrently; each mirrored line is id-prefixed" >&2
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    while IFS=$'\t' read -r id pdf final; do
        mark=""
        [[ -s "$final" && "$FORCE" != "1" ]] && mark="  # SKIP (exists)"
        echo "$id  $pdf -> $final$mark"
    done <"$MANIFEST"
    echo
    echo "(dry run - no PDF extraction or Codex session executed)"
    exit 0
fi

# _status.tsv is no longer truncated here; it is regenerated from the ledger
# once the workers are done, so a run that dies still leaves the ledger intact.
export RB_OUT_DIR="$OUT_DIR" RB_STATUS="$STATUS" RB_CODEX="$CODEX_BIN" RB_UV="$UV_BIN" \
       RB_MODEL="$MODEL" RB_REVIEW_MODEL="$REVIEW_MODEL" RB_VERBOSITY="$VERBOSITY" \
       RB_PROGRESS="$PROGRESS" RB_FORCE="$FORCE" RB_RETRIES="$RETRIES" \
       RB_RETRY_SLEEP="$RETRY_SLEEP" \
       RB_LEDGER="$LEDGER" RB_BUDGET="$BUDGET" RB_STOP="$STOP" RB_JQ="$JQ_BIN" \
       RB_RUN_ID="$RB_RUN_ID" RB_RUN_START="$(date +%s)" RB_WORKERS="$WORKERS" \
       RB_SESSION_BUDGET_S="$SESSION_BUDGET_S" RB_SESSION_TOKENS="$SESSION_TOKENS" \
       RB_SOFT_PCT="$SOFT_PCT" RB_HARD_PCT="$HARD_PCT" \
       RB_HEADROOM_FACTOR="$HEADROOM_FACTOR" RB_PROBE="$PROBE" \
       RB_KEEP_JSONL="$KEEP_JSONL"

START=$SECONDS
cut -f2 "$MANIFEST" | tr '\n' '\0' | xargs -0 -P "$WORKERS" -n 1 "$REPO_DIR/run_batch.sh" --run-one
ELAPSED=$((SECONDS - START))

# The 4-column shape the summary and the notifier already read, rebuilt from
# this run's ledger rows.
awk -F'\t' -v r="$RB_RUN_ID" '$1==r { print $3"\t"$4"\t"$6"\t"$7 }' "$LEDGER" >"$STATUS"

OK="$(awk -F'\t' '$2=="OK"' "$STATUS" | wc -l | tr -d ' ')"
SKIPPED="$(awk -F'\t' '$2=="SKIP"' "$STATUS" | wc -l | tr -d ' ')"
SCANNED="$(awk -F'\t' '$2=="UNSUPPORTED_SCAN"' "$STATUS" | wc -l | tr -d ' ')"
FAILED="$(awk -F'\t' '$2=="FAIL"' "$STATUS" | wc -l | tr -d ' ')"
FAILED_IDS="$(awk -F'\t' '$2=="FAIL"{print $1}' "$STATUS" | tr '\n' ' ')"
LIMIT_HIT="$(awk -F'\t' '$2=="LIMIT_HIT"' "$STATUS" | wc -l | tr -d ' ')"
DEFERRED="$(awk -F'\t' '$2=="DEFERRED"' "$STATUS" | wc -l | tr -d ' ')"
# Work still to do: DEFERRED rows (admission refused them) plus any PDF the
# drain gate stopped xargs from dispatching at all, which produces no row.
LEFT=$((TOTAL - OK - SKIPPED - SCANNED - FAILED - LIMIT_HIT))
[[ "$LEFT" -lt 0 ]] && LEFT=0

[[ "$PROGRESS" == "quiet" ]] || echo
echo "done: $OK ok, $SKIPPED skipped, $SCANNED unsupported scan, $FAILED failed (elapsed $((ELAPSED / 60))m$((ELAPSED % 60))s)"
echo "status: $STATUS"
if [[ "$SCANNED" -gt 0 ]]; then
    echo "scanned PDFs require OCR; see $OUT_DIR/logs/<id>.log"
fi
if [[ "$FAILED" -gt 0 ]]; then
    echo "failed: $FAILED_IDS -> see $OUT_DIR/logs/<id>.log"
    echo "re-run the same command to retry failures"
fi

STATE=done
REASON=""
if [[ -e "$STOP" ]]; then
    STATE=drained
    REASON="$(RB_STOP="$STOP" rb_stop_reason)"
    echo "drained: $REASON"
    echo "left: $LEFT not started; re-run with --ignore-stop once quota resets"
    echo "check quota: tools/budget.sh --probe"
fi

rb_notify_run "$STATE" "$OK" "$SKIPPED" "$SCANNED" "$FAILED" "$LIMIT_HIT" "$DEFERRED" \
    "$LEFT" "$ELAPSED" "$REASON"

[[ "$STATE" == "drained" ]] && exit 3
[[ "$FAILED" -gt 0 ]] && exit 1
exit 0
