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
#   ./run_batch.sh --assignment-label 0 --limit 50   # this machine's share only
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

# INPUT_DIR="/Users/yixuanfeng/Desktop/web-download/downloads/nejm" # TODO: change this input dir
INPUT_DIR="/Users/yixuanfeng/Desktop/trial-codex/samples"
OUT_DIR="test_result"
# Work split across machines: a label,id CSV whose id is <journal>-<article-id>. Only
# read when --assignment-label asks for it, so a machine without the file still runs.
ASSIGNMENT_FILE="/Users/yixuanfeng/Desktop/trial-codex/assignment.csv"
ASSIGNMENT_LABEL=""
LIMIT=3
OFFSET=0
WORKERS=3
RETRIES=1
RETRY_SLEEP=15
MODEL="gpt-5.6-terra"
REVIEW_MODEL="gpt-5.6-luna"
# Reasoning effort for MODEL. Distinct from VERBOSITY below, which is SAP output
# detail carried in the prompt and never reaches the model's reasoning parameter.
EFFORT="medium"
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
    sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --input-dir DIR   root searched recursively for *_protocol*.pdf
  --out-dir DIR     output directory (default: test_result)
  --assignment-label L  process only the PDFs labeled L in the assignment CSV;
                        comma-separated for several (0,2). Applied before --offset
                        and --limit, and emitted in the CSV's row order, so those
                        page through this machine's share as the CSV lists it.
                        Default: no filter, every PDF under --input-dir, path order
  --assignment-file F   the label,id CSV, id being <journal>-<article-id>
                        (default: assignment.csv in the repo root)
  --limit N         number of PDFs to process (default: 3)
  --offset N        skip the first N PDFs (default: 0)
  --workers N       parallel primary Codex sessions (default: 3; each uses a reviewer)
  --retries N       retries after an agent/lint failure (default: 1)
  --model NAME      model passed to `codex exec` (default: gpt-5.6-terra)
  --review-model N  reviewer subagent model (default: gpt-5.6-luna)
  --effort LEVEL    reasoning effort for --model (default: medium)
  --verbosity LEVEL SAP detail, not model reasoning: low, medium, or high (default: high)
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

# derive_id PDF_PATH  ->  <journal>++<article-id>++<file-id>
# The corpus is laid out as <root>/<journal>/<article-id>/<file-id>.pdf, so the id is
# the last three path components. Read from the path's tail rather than relative to
# --input-dir, so pointing a run at the corpus root or at one journal directory yields
# the same id, and a worker needs nothing but the PDF path it is handed.
#
# The whole path is load-bearing: supplement filenames repeat across articles (every
# Lancet supplement is mmc1_protocol.pdf), so a basename-derived id collides and the
# manifest dedup silently drops the duplicates.
derive_id() {
    local abs dir stem article journal id
    stem="$(basename "$1")"
    stem="${stem%.pdf}"
    dir="$(dirname "$1")"
    abs="$(cd "$dir" 2>/dev/null && pwd)" && dir="$abs"

    article="$(basename "$dir")"
    journal="$(basename "$(dirname "$dir")")"

    # A path shallower than <journal>/<article-id>/<file>.pdf contributes what it has.
    id="$stem"
    [[ "$article" == "/" || "$article" == "." ]] || id="${article}++${id}"
    [[ "$journal" == "/" || "$journal" == "." ]] || id="${journal}++${id}"

    # ':' is legal in a POSIX filename but Finder renders it as '/', and some article
    # directories carry one (10.1136:bmj.j5157). Fold it the way the DOI slash is
    # already folded upstream.
    printf '%s' "${id//:/_}"
}

# rb_filter_assignment
# Reads PDF paths on stdin and passes through only those assigned to this machine,
# emitting them in the assignment file's own row order rather than in path order.
# A no-op unless --assignment-label was given, so the default run is unchanged.
#
# Order matters because --offset and --limit page through this output: following the
# CSV means "the first 200 rows of my share", which is the same set of PDFs on every
# machine and stays stable when the corpus directory gains or loses a file.
#
# The CSV key is <journal>-<article-id>, which is the first two components of the id
# derive_id builds, so the two are read off the same path and stay in agreement.
rb_filter_assignment() {
    [[ -n "$ASSIGNMENT_LABEL" ]] || { cat; return 0; }

    awk -F/ -v csv="$ASSIGNMENT_FILE" -v want="$ASSIGNMENT_LABEL" '
        BEGIN {
            n = split(want, w, ",")
            for (i = 1; i <= n; i++) wanted[w[i]] = 1
            while ((getline line < csv) > 0) {
                # The file is CRLF. Without this every id carries a trailing \r and
                # nothing matches, silently selecting no work at all.
                gsub(/\r/, "", line)
                p = index(line, ",")
                if (p == 0) continue
                label = substr(line, 1, p - 1)
                id = substr(line, p + 1)
                gsub(/:/, "_", id)
                known[id] = 1
                if ((label in wanted) && !(id in mine)) {
                    mine[id] = 1
                    order[++ranked] = id
                }
            }
        }
        {
            key = (NF >= 3) ? $(NF - 2) "-" $(NF - 1) : ""
            gsub(/:/, "_", key)
            # Held rather than printed, so the CSV can decide the order below. An
            # article with two protocol PDFs keeps both, in the order they arrived.
            if (key in mine) held[key] = held[key] $0 "\n"
            # Labeled for another machine: expected, and the whole point of the flag.
            # No row at all is a gap between the corpus and the CSV, so say so.
            else if (!(key in known)) unknown++
        }
        END {
            for (i = 1; i <= ranked; i++)
                if (order[i] in held) printf "%s", held[order[i]]
            if (unknown)
                printf "warn: %d pdf(s) have no row in %s and were excluded\n", \
                    unknown, csv > "/dev/stderr"
        }
    '
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

# rb_review_has_line REVIEW LINE
# Whole-line match against the review file, forgiving trailing whitespace only.
# Wording and case must still match exactly: AGENTS.md makes these literals a
# contract with the reviewer, so only an invisible difference is worth excusing.
#
# One awk process, deliberately not `sed | grep -q`: this script runs under
# `set -o pipefail`, and a -q grep that matches early closes the pipe, so sed
# dies of SIGPIPE and the pipeline reports failure for a line that is present.
# The earlier the match, the more reliably it breaks -- a first-line match all
# but always does. ENVIRON rather than -v, which would expand backslashes.
rb_review_has_line() {
    RB_WANT="$2" awk '
        BEGIN { want = ENVIRON["RB_WANT"] }
        { line = $0; sub(/[[:space:]]+$/, "", line); if (line == want) { found = 1; exit } }
        END { exit !found }
    ' "$1"
}

run_one() {
    local pdf="$1"
    local id final draft evidence review extracted log start elapsed bytes page_count
    local attempt rc lint_rc evidence_rc review_rc prompt jsonl lastmsg usage cls
    local declared verdict
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
    final="$RB_OUT_DIR/${id}.md"
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
        #
        # The rejected review is moved aside rather than discarded. Truncating it in
        # place destroys the only record of why the gate below refused it, so a retry
        # that then succeeds leaves the original failure permanently undiagnosable.
        # Archives never feed the gate; they exist only to be read afterwards.
        if [[ -s "$review" ]]; then
            if ((attempt > 0)); then
                mv -f "$review" "${review}.attempt${attempt}"
            else
                mv -f "$review" "${review}.stale"
            fi
        fi
        : >"$review"

        # rb_run_codex overwrites the transcript, which costs the rejected attempt's
        # reviewer exchange for the same reason. Keep it alongside its review.
        if ((attempt > 0)) && [[ -s "$jsonl" ]]; then
            mv -f "$jsonl" "${jsonl}.attempt${attempt}"
        fi

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
        # -c parses its value as TOML, so the level is passed quoted rather than
        # leaning on the bare-word-to-literal-string fallback.
        codex_args+=(-c "model_reasoning_effort=\"$RB_EFFORT\"")

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
        # Checked one requirement at a time. A single combined test reports only that
        # the review was rejected, which is not enough to tell a reviewer that ran as
        # the wrong model from one that merely mislabelled its checklist heading.
        if [[ ! -s "$review" ]]; then
            echo "error: Codex did not produce review: $review" >>"$log"
        else
            review_rc=0
            if ! rb_review_has_line "$review" "Reviewer model: $RB_REVIEW_MODEL"; then
                review_rc=1
                declared="$(grep -m1 '^Reviewer model:' "$review")"
                echo "error: review declares the wrong reviewer model: expected 'Reviewer model: $RB_REVIEW_MODEL', found '${declared:-<no Reviewer model: line>}': $review" >>"$log"
            fi
            if ! rb_review_has_line "$review" '## Final field checklist'; then
                review_rc=1
                echo "error: review is missing the exact line '## Final field checklist': $review" >>"$log"
            fi
            if ! rb_review_has_line "$review" 'VERDICT: PASS'; then
                review_rc=1
                verdict="$(grep -m1 'VERDICT' "$review")"
                echo "error: review is missing the standalone line 'VERDICT: PASS'${verdict:+ (nearest VERDICT line: '$verdict')}: $review" >>"$log"
            fi
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
            [[ "$RB_KEEP_JSONL" == "always" ]] || rm -f "$jsonl" "$jsonl".attempt*
            rb_append_ledger "$id" OK "$((attempt + 1))" "$elapsed" "$bytes" \
                "$in_tok" "$out_tok" "$tot_tok" "-"
            progress job "[ok]   $id  ${elapsed}s  ${bytes}B"
            return 0
        fi
        progress step "[check] $id  codex=$rc evidence=$evidence_rc review=$review_rc lint=$lint_rc"
    done

    elapsed=$((SECONDS - start))
    [[ "$RB_KEEP_JSONL" == "never" ]] && rm -f "$jsonl" "$jsonl".attempt*
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
        --assignment-label) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; ASSIGNMENT_LABEL="$2"; shift 2 ;;
        --assignment-file)  [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; ASSIGNMENT_FILE="$2"; shift 2 ;;
        --limit)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; LIMIT="$2"; shift 2 ;;
        --offset)    [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; OFFSET="$2"; shift 2 ;;
        --workers)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; WORKERS="$2"; shift 2 ;;
        --retries)   [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; RETRIES="$2"; shift 2 ;;
        --model)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; MODEL="$2"; shift 2 ;;
        --review-model) [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; REVIEW_MODEL="$2"; shift 2 ;;
        --effort)    [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; EFFORT="$2"; shift 2 ;;
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
# The union of what the model catalog offers. gpt-5.6-terra takes all six; another
# --model may take fewer (gpt-5.5 stops at xhigh), and Codex rejects it at request
# time. Validating the union keeps this script from second-guessing the catalog.
case "$EFFORT" in
    low|medium|high|xhigh|max|ultra) ;;
    *) echo "effort must be one of: low, medium, high, xhigh, max, ultra" >&2; exit 2 ;;
esac
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

# Checked only when the filter is actually requested. A label that matches no row
# would otherwise select nothing and report it as "no PDFs found", which points at
# the input directory rather than at the typo.
if [[ -n "$ASSIGNMENT_LABEL" ]]; then
    [[ -r "$ASSIGNMENT_FILE" ]] || { echo "assignment file not readable: $ASSIGNMENT_FILE" >&2; exit 1; }
    AVAILABLE_LABELS="$(tr -d '\r' <"$ASSIGNMENT_FILE" | awk -F, 'NR > 1 && $1 != "" { print $1 }' | sort -u)"
    IFS=, read -ra REQUESTED_LABELS <<<"$ASSIGNMENT_LABEL"
    for label in "${REQUESTED_LABELS[@]}"; do
        [[ -n "$label" ]] || { echo "assignment-label must not contain an empty value" >&2; exit 2; }
        grep -Fxq -- "$label" <<<"$AVAILABLE_LABELS" || {
            echo "no rows labeled '$label' in $ASSIGNMENT_FILE" >&2
            echo "labels present: $(tr '\n' ' ' <<<"$AVAILABLE_LABELS")" >&2
            exit 2
        }
    done
fi

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
    printf '%s\t%s\t%s\n' "$id" "$pdf" "$OUT_DIR/${id}.md"
done < <(
    find "$INPUT_DIR" -type f -name '*_protocol*.pdf' -print \
        | sort \
        | rb_filter_assignment \
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
    echo "no *_protocol*.pdf found under $INPUT_DIR (offset=$OFFSET limit=$LIMIT${ASSIGNMENT_LABEL:+ assignment-label=$ASSIGNMENT_LABEL})" >&2
    exit 1
fi

if [[ "$PROGRESS" != "quiet" ]]; then
    echo "input   : $INPUT_DIR"
    [[ -z "$ASSIGNMENT_LABEL" ]] || echo "assigned: label $ASSIGNMENT_LABEL  ($ASSIGNMENT_FILE)"
    echo "output  : $OUT_DIR"
    echo "harness : $CODEX_BIN${MODEL:+  (model: $MODEL)}"
    echo "effort  : $EFFORT"
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
       RB_MODEL="$MODEL" RB_REVIEW_MODEL="$REVIEW_MODEL" RB_EFFORT="$EFFORT" \
       RB_VERBOSITY="$VERBOSITY" \
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
