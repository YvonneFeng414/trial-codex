#!/usr/bin/env bash
#
# Heuristic completeness score for SAP markdowns, one `codex exec` session per file.
#
#   tools/eval_completeness.sh FILE.md [FILE.md ...]
#   tools/eval_completeness.sh --dir Benchmark_merged/2 --n 10 --seed 42
#
# Each session reads one SAP markdown (never the protocol or PDF), classifies every
# template field, and returns one TSV row. Rows are appended to --out, which is then
# sorted by stat_score. Files already in --out are skipped, and a random sample is
# drawn only from files not yet scored, so re-running with --n draws new files.
#
# Columns: file stat_score overall_score filled partial not_reported not_applicable main_gaps
#   score = (filled + 0.5*partial) / (fields - not_applicable)
#   stat  = sections 2.3-2.6, 3.1-3.3, 5.3-5.7; overall = every template field

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CODEX_HOME="$REPO_DIR/.codex-home"

DIR=""
N=10
SEED=""
OUT="completeness.tsv"
MODEL="gpt-5.6-terra"
EFFORT="low"
DRY_RUN=0
FILES=()

usage() {
    awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"
    cat <<'EOF'

Options:
  --dir DIR       sample markdowns from DIR instead of taking FILE arguments
  --n N           sample size with --dir (default: 10)
  --seed S        random seed for the sample (default: random, printed)
  --out FILE      output TSV (default: completeness.tsv)
  --model NAME    model passed to `codex exec` (default: gpt-5.6-terra)
  --effort LEVEL  reasoning effort (default: low)
  --dry-run       print the files and the prompt for the first one; run nothing
  -h, --help      show this message
EOF
}

while (($#)); do
    case "$1" in
        --dir) DIR="$2"; shift 2 ;;
        --n) N="$2"; shift 2 ;;
        --seed) SEED="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --effort) EFFORT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) FILES+=("$1"); shift ;;
    esac
done

command -v codex >/dev/null || { echo "codex executable not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }

HEADER=$'file\tstat_score\toverall_score\tfilled\tpartial\tnot_reported\tnot_applicable\tmain_gaps'

scored() { [[ -f "$OUT" ]] && cut -f1 "$OUT" | grep -Fxq -- "$1"; }

# Random sample from DIR, excluding files already in OUT.
SAMPLE_PY='
import os, random, sys
d, n, seed, out = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
done = {l.split("\t", 1)[0] for l in open(out)} if os.path.exists(out) else set()
pool = sorted(f for f in os.listdir(d) if f.endswith(".md") and f not in done)
random.seed(seed)
for f in random.sample(pool, min(n, len(pool))):
    print(os.path.join(d, f))
'

if [[ -n "$DIR" ]]; then
    ((${#FILES[@]} == 0)) || { echo "give either --dir or FILE arguments, not both" >&2; exit 2; }
    [[ -n "$SEED" ]] || SEED=$RANDOM
    echo "sampling $N from $DIR (seed $SEED)" >&2
    while IFS= read -r f; do
        FILES+=("$f")
    done < <(python3 -c "$SAMPLE_PY" "$DIR" "$N" "$SEED" "$OUT")
fi
((${#FILES[@]})) || { echo "no files to score" >&2; usage >&2; exit 2; }

build_prompt() {
    cat <<EOF
This is a scoring task, not SAP generation. Do not follow any protocol-to-SAP workflow,
do not spawn subagents, and do not edit any file.

Read only this SAP markdown: $1
Do not open the protocol, any PDF, or any other file.

Classify every field of the SAP template below. Count each repeated 2.3 sample-size block
and each repeated 5.1/5.2 outcome block (Definition, Measurement Units, Timing of
Measurement, Transformations / Derived Variables) once per repeat. A narrative section
without bullet fields (1.1, 2.4, 4.1, 4.2, 4.3, 5.5) is one field.

Template fields:
- 1.1 Background and Rationale; 1.2 Primary / Secondary / Exploratory Objectives
- 2.1 Design Type, Allocation Ratio, Intervention Summary
- 2.2 Method, Stratification Factors, Minimization or Blocking
- 2.3 Planned Sample Size, Outcome Used for Calculation, Outcome Type, Assumed Control-Arm
  Value, Assumed Treatment-Arm Value, Target Difference / Effect Size, Variability
  Assumption, Alpha, Power, Statistical Test Assumed, Required Number of Events,
  Non-Inferiority / Equivalence Margin, Dropout / Non-Adherence Inflation, Design Effect /
  Clustering, Basis for Assumptions, Sample Size Re-Estimation / Amendments, Software Used
  for Calculation, Sample Size Rationale
- 2.4 Hypothesis Framework
- 2.5 Interim Analyses Planned, Stopping Guidelines, Alpha Spending or Adjustment
- 2.6 Final Analysis Timing, Outcome Assessment Schedule
- 3.1 Alpha Level, Multiplicity Adjustment, Confidence Intervals
- 3.2 Adherence Definition, Adherence Assessment, Protocol Deviation Definition, Protocol
  Deviation Reporting
- 3.3 Intention-to-Treat, Per Protocol, Other Populations
- 4.1 Screening Data; 4.2 Eligibility Criteria Summary; 4.3 Recruitment
- 4.4 Withdrawal Levels, Withdrawal Data Collection, Reasons for Withdrawal
- 4.5 Baseline Variables, Summary Methods
- 5.1 / 5.2 outcome blocks (see above)
- 5.3 Primary Analysis Methods, Covariate Adjustment, Assumption Checks, Alternative Methods
  if Assumptions Fail, Sensitivity Analyses, Subgroup Analyses
- 5.4 Missing Data Assumptions, Handling Methods
- 5.5 Additional Analyses
- 5.6 Adverse Event Definitions, Safety Analysis Methods, Grading Scales and Causality
- 5.7 Statistical Software, Other Referenced Documents

Classes (ignore the italic *(Source: ...)* citations when judging content):
- filled: substantive content.
- partial: some content, but the text says part of the field is not reported.
- not_reported: "Not reported in protocol", "Deferred to SAP", or equivalent, with no
  other content. A field missing from the markdown also counts as not_reported.
- not_applicable: "Not applicable" because the field cannot apply to this design (e.g. a
  non-inferiority margin in a superiority trial, a design effect in a non-clustered trial,
  a number of events for a continuous outcome).

Scores, rounded to 3 decimals:
- score = (filled + 0.5 * partial) / (fields - not_applicable)
- stat_score uses only fields in sections 2.3-2.6, 3.1-3.3 and 5.3-5.7
- overall_score uses every field

Reply with exactly one line and nothing else: these 7 values separated by TAB characters,
with counts over all fields:
stat_score	overall_score	filled	partial	not_reported	not_applicable	main_gaps
main_gaps is a short semicolon-separated list of the most important not_reported or
partial fields (e.g. "2.3 Software; 3.3 Per Protocol"), or "none". No tabs inside it.
EOF
}

if ((DRY_RUN)); then
    printf '%s\n' "${FILES[@]}"
    echo "----- prompt for ${FILES[0]} -----"
    build_prompt "${FILES[0]}"
    exit 0
fi

[[ -s "$OUT" ]] || printf '%s\n' "$HEADER" >"$OUT"
LOG_DIR="${OUT%.tsv}.logs"
mkdir -p "$LOG_DIR"

# Last non-empty line of the reply, stripped of code fences, if it is a valid row.
parse_row() {
    local line
    line="$(grep -v '^[[:space:]]*```' "$1" | awk 'NF { l = $0 } END { print l }' | tr -d '\r')"
    printf '%s\n' "$line" | awk -F'\t' '
        NF == 7 && $1 ~ /^[01](\.[0-9]+)?$/ && $2 ~ /^[01](\.[0-9]+)?$/ &&
        $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ && $6 ~ /^[0-9]+$/ { print; ok = 1 }
        END { exit !ok }'
}

ok=0 failed=0 skipped=0 i=0
for f in "${FILES[@]}"; do
    i=$((i + 1))
    name="$(basename "$f")"
    if scored "$name"; then
        echo "[$i/${#FILES[@]}] skip  $name (already in $OUT)" >&2
        skipped=$((skipped + 1)); continue
    fi
    [[ -f "$f" ]] || { echo "[$i/${#FILES[@]}] FAIL  $name (not found)" >&2; failed=$((failed + 1)); continue; }

    abs="$(cd "$(dirname "$f")" && pwd)/$name"
    log="$LOG_DIR/${name%.md}.log"
    reply="$LOG_DIR/${name%.md}.reply"
    row=""
    for attempt in 1 2; do
        : >"$reply"
        build_prompt "$abs" | codex exec -C "$REPO_DIR" --skip-git-repo-check --ephemeral \
            --sandbox read-only --color never -o "$reply" --model "$MODEL" \
            -c "model_reasoning_effort=\"$EFFORT\"" -c "project_doc_max_bytes=0" - \
            >>"$log" 2>&1
        row="$(parse_row "$reply")" && break
        row=""
    done

    if [[ -n "$row" ]]; then
        printf '%s\t%s\n' "$name" "$row" >>"$OUT"
        echo "[$i/${#FILES[@]}] ok    $name  $(printf '%s' "$row" | cut -f1,2)" >&2
        ok=$((ok + 1))
    else
        echo "[$i/${#FILES[@]}] FAIL  $name (no valid row; see $log)" >&2
        failed=$((failed + 1))
    fi
done

# Sort rows by stat_score, then overall_score, keeping the header first.
tmp="$(mktemp "${OUT}.XXXXXX")"
{ head -n 1 "$OUT"; tail -n +2 "$OUT" | sort -t$'\t' -k2,2gr -k3,3gr; } >"$tmp" && mv "$tmp" "$OUT"

echo "done: $ok scored, $skipped skipped, $failed failed -> $OUT" >&2
((failed == 0))
