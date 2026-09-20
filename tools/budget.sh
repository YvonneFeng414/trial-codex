#!/usr/bin/env bash
#
# Budget governor for long batch runs. Answers one question before each atomic
# job starts - "is there enough quota and wall clock left to FINISH this?" - and
# records the answer so the next session resumes without re-deriving anything.
#
# As a library, which is how run_batch.sh consumes it:
#
#   . tools/budget.sh
#   rb_admit                      # 0 go, 10 drain gracefully, 11 hard stop
#   rb_append_ledger ID STATE ATTEMPT ELAPSED BYTES IN OUT TOTAL DETAIL
#   rb_trip_stop "reason"         # first writer wins; every worker then drains
#
# As a command, useful on its own:
#
#   tools/budget.sh --probe       # live rate-limit snapshot, costs zero tokens
#   tools/budget.sh --report      # what the cumulative ledger knows
#
# Four independent signals, so the run stays correct if any subset is missing:
#
#   L0  quota errors in codex output      reactive hard stop, never a retry
#   L1  usedPercent from the app-server   proactive percent gate
#   L2  turn.completed.usage tokens       proactive token budget
#   L3  wall clock + p90 from the ledger  backstop; the only one that works cold
#
# Two properties are load-bearing and easy to break by accident:
#
#   1. Every shared file is append-only and every append is a single printf, so
#      parallel workers need no lock. macOS has no flock(1); do not add one.
#   2. The stop-file is created with O_EXCL (set -C). Exactly one worker writes
#      the reason; everyone else treats mere existence as authoritative.

# Config. Every default yields to a value the caller has already set, so
# run_batch.sh's flags win and a fixture can point these at a sandbox.
: "${RB_OUT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test_result}"
: "${RB_LEDGER:=$RB_OUT_DIR/_ledger.tsv}"
: "${RB_BUDGET:=$RB_OUT_DIR/_budget.tsv}"
: "${RB_STOP:=$RB_OUT_DIR/.stop}"
: "${RB_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)-$$}"
: "${RB_RUN_START:=$(date +%s)}"
: "${RB_SESSION_BUDGET_S:=0}"        # 0 = unlimited
: "${RB_SESSION_TOKENS:=0}"          # 0 = unlimited
: "${RB_SOFT_PCT:=80}"
: "${RB_HARD_PCT:=92}"
: "${RB_HEADROOM_FACTOR:=1.0}"
: "${RB_PROBE:=auto}"                # auto | on | off
: "${RB_PROBE_WAIT:=3}"              # seconds to hold the app-server stdin open
: "${RB_KEEP_JSONL:=on-error}"       # always | on-error | never
: "${RB_WORKERS:=1}"
: "${RB_CODEX:=$(command -v codex || echo codex)}"
: "${RB_JQ:=$(command -v jq || echo jq)}"
: "${RB_PROGRESS:=normal}"

# Cold-start estimates, deliberately above every value observed so far (3313s,
# 200695 tokens) so the very first run errs toward stopping early.
: "${RB_P90_ELAPSED_DEFAULT:=3600}"
: "${RB_P90_TOKENS_DEFAULT:=250000}"
: "${RB_PCT_PER_JOB_DEFAULT:=3}"

# Quota signatures. A typed variant may surface only as rendered prose, and a
# transport-level 429 may produce no JSONL event at all, so both the structured
# events and the tail of the human log are searched.
: "${RB_QUOTA_RE:=usage_limit_exceeded|rate_limit_exceeded|usage_limit_reached|quota_exceeded|session_budget_exceeded|workspace_owner_usage_limit_reached|workspace_member_usage_limit_reached|hit your usage limit|usage limit reached|out of credits}"

RB_ADMIT_REASON=""
RB_QUOTA_VARIANT=""
RB_PROBE_DISABLED=0

rb_now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# rb_field VALUE [MAXLEN] -- make a string safe for one TSV cell. Bounding the
# length keeps a row well under PIPE_BUF, which is what makes the lockless
# append safe.
rb_field() {
    local v="${1-}" max="${2:-120}"
    v="$(printf '%s' "$v" | tr '\t\r\n' '   ')"
    printf '%s' "${v:0:$max}"
    [[ "${#v}" -gt "$max" ]] && printf '...'
    return 0
}

# rb_append_ledger ID STATE ATTEMPT ELAPSED BYTES IN_TOK OUT_TOK TOTAL_TOK DETAIL
# 11 columns, append-only, never truncated. This is the durable cross-session
# record; _status.tsv is regenerated from it.
rb_append_ledger() {
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$RB_RUN_ID" "$(rb_now_iso)" "$1" "$2" "${3:-0}" "${4:-0}" "${5:-0}" \
        "${6:-0}" "${7:-0}" "${8:-0}" "$(rb_field "${9:--}")" >>"$RB_LEDGER"
    return 0
}

# rb_append_budget SOURCE PRIMARY_PCT PRIMARY_RESETS SECONDARY_PCT SECONDARY_RESETS TOTAL_TOK DETAIL
# SOURCE is probe | event | error | tokens. Resets are unix epoch seconds, or -.
rb_append_budget() {
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(rb_now_iso)" "$RB_RUN_ID" "$1" "${2:--}" "${3:--}" "${4:--}" \
        "${5:--}" "${6:-0}" "$(rb_field "${7:--}")" >>"$RB_BUDGET"
    return 0
}

# rb_trip_stop REASON -- O_EXCL create, so exactly one worker records why.
rb_trip_stop() {
    if ( set -C; : >"$RB_STOP" ) 2>/dev/null; then
        printf '%s\t%s\t%s\n' "$(rb_now_iso)" "$$" "$(rb_field "${1:-unspecified}")" >"$RB_STOP"
    fi
    return 0
}

rb_stop_reason() {
    [[ -s "$RB_STOP" ]] || { printf 'drain requested'; return 0; }
    awk -F'\t' 'NR==1 { print $3 }' "$RB_STOP" 2>/dev/null | head -1
    return 0
}

# rb_p90 COL DEFAULT -- p90 of an OK row column over the whole cumulative
# ledger. One awk|sort|sed over 1334 rows is ~15ms, so this runs per job, not
# per check.
rb_p90() {
    local vals n k
    vals="$(awk -F'\t' -v c="$1" '$4=="OK" && ($c+0)>0 { print $c+0 }' "$RB_LEDGER" 2>/dev/null | sort -n)"
    n="$(printf '%s\n' "$vals" | grep -c '^[0-9]' 2>/dev/null)"
    [[ "${n:-0}" -eq 0 ]] && { printf '%s' "$2"; return 0; }
    k=$(( (n * 9 + 9) / 10 ))
    printf '%s' "$(printf '%s\n' "$vals" | sed -n "${k}p")"
    return 0
}
rb_p90_elapsed() { rb_p90 6 "$RB_P90_ELAPSED_DEFAULT"; }
rb_p90_tokens()  { rb_p90 10 "$RB_P90_TOKENS_DEFAULT"; }

rb_run_tokens() {
    awk -F'\t' -v r="$RB_RUN_ID" '$1==r { s += $10 } END { print s+0 }' "$RB_LEDGER" 2>/dev/null
    return 0
}

# Last budget row carrying a usable primary percentage.
rb_last_budget() {
    awk -F'\t' '$4 ~ /^[0-9]+(\.[0-9]+)?$/ { l = $0 } END { if (l != "") print l }' \
        "$RB_BUDGET" 2>/dev/null
    return 0
}

# Largest positive jump in primary percent between consecutive observations of
# the same window. Inert until L1 has produced at least two readings.
rb_pct_per_job() {
    local d
    d="$(awk -F'\t' '
        $4 ~ /^[0-9]+(\.[0-9]+)?$/ {
            if (prev != "" && $5 == prevreset && ($4 - prev) > mx) mx = $4 - prev
            prev = $4; prevreset = $5
        }
        END { if (mx > 0) printf "%d", (mx == int(mx) ? mx : int(mx) + 1) }' \
        "$RB_BUDGET" 2>/dev/null)"
    printf '%s' "${d:-$RB_PCT_PER_JOB_DEFAULT}"
    return 0
}

# rb_probe_rate_limits -- ask the app-server for the live snapshot. Costs zero
# model tokens. Prints "PPCT PRESET SPCT SRESET PLAN" or nothing.
#
# The request pair must stay on stdin for a moment: closing stdin immediately
# makes the server shut down before the account read returns. Command
# substitution waits for the whole pipeline anyway, so the hold is a plain
# bounded sleep rather than machinery to exit early.
rb_probe_rate_limits() {
    [[ "$RB_PROBE" == "off" || "$RB_PROBE_DISABLED" == "1" ]] && return 1
    local raw
    raw="$( { printf '%s\n' \
            '{"id":1,"method":"initialize","params":{"clientInfo":{"name":"run_batch","version":"0.1.0"}}}' \
            '{"id":2,"method":"account/rateLimits/read","params":null}'
            sleep "$RB_PROBE_WAIT"
        } | "$RB_CODEX" app-server 2>/dev/null \
          | "$RB_JQ" -r 'select(.id==2) | .result.rateLimits
              | [ (.primary.usedPercent // "-"), (.primary.resetsAt // "-"),
                  (.secondary.usedPercent // "-"), (.secondary.resetsAt // "-"),
                  (.planType // "-") ] | @tsv' 2>/dev/null | head -1 )"
    if [[ -z "$raw" ]]; then
        RB_PROBE_DISABLED=1          # self-disable; L0/L2/L3 still govern
        return 1
    fi
    printf '%s' "$raw"
    return 0
}

# rb_scan_usage JSONL -> "IN OUT TOTAL". Sums every turn.completed, which with
# --enable multi_agent includes the reviewer subagent's turns: that is the whole
# cost of the job, which is what a token budget needs.
rb_scan_usage() {
    "$RB_JQ" -r 'select(.type=="turn.completed") | .usage
        | [ (.input_tokens // 0), (.output_tokens // 0), (.reasoning_output_tokens // 0) ]
        | @tsv' "$1" 2>/dev/null \
    | awk -F'\t' '{ i += $1; o += $2 + $3 } END { printf "%d\t%d\t%d\n", i+0, o+0, i+o }'
    return 0
}

# rb_scan_budget JSONL -- codex 0.152 does NOT emit rate_limits on the exec
# stream (verified against the binary's event table and a live run). This scans
# for it anyway, at any depth and under either spelling, so that a future codex
# which does emit it is picked up with no code change here.
rb_scan_budget() {
    "$RB_JQ" -r 'try (.. | objects | select(has("rate_limits") or has("rateLimits"))
        | (.rate_limits // .rateLimits)
        | [ (.primary.used_percent // .primary.usedPercent // "-"),
            (.primary.resets_at // .primary.resetsAt // "-"),
            (.secondary.used_percent // .secondary.usedPercent // "-"),
            (.secondary.resets_at // .secondary.resetsAt // "-") ] | @tsv)
        catch empty' "$1" 2>/dev/null | tail -n 1
    return 0
}

# rb_classify_codex RC JSONL LOG -> 0 clean, 1 ordinary failure, 2 quota.
# Sets RB_QUOTA_VARIANT. A quota stop must never be mistaken for a failure: it
# should not burn a retry and must not be recorded as FAIL.
rb_classify_codex() {
    local rc="$1" jsonl="$2" log="$3" msgs
    RB_QUOTA_VARIANT=""
    msgs="$(
        "$RB_JQ" -r 'select(.type=="error" or .type=="turn.failed")
            | (.message // .error.message // empty)' "$jsonl" 2>/dev/null
        tail -c 65536 "$log" 2>/dev/null
    )"
    if printf '%s' "$msgs" | grep -qiE "$RB_QUOTA_RE"; then
        RB_QUOTA_VARIANT="$(printf '%s' "$msgs" | grep -oiE "$RB_QUOTA_RE" | head -1)"
        return 2
    fi
    [[ "$rc" -eq 0 ]] && return 0
    return 1
}

# rb_admit -- may this worker START a new job? Sets RB_ADMIT_REASON.
# 0 go, 10 drain gracefully, 11 hard stop. Checks run in cost order.
rb_admit() {
    RB_ADMIT_REASON=""
    local now remaining p90 need pct reset spct line tok

    [[ -e "$RB_STOP" ]] && { RB_ADMIT_REASON="stop-file: $(rb_stop_reason)"; return 10; }

    now="$(date +%s)"
    if [[ "$RB_SESSION_BUDGET_S" -gt 0 ]]; then
        remaining=$(( RB_SESSION_BUDGET_S - (now - RB_RUN_START) ))
        if [[ "$remaining" -le 0 ]]; then
            RB_ADMIT_REASON="session budget spent"
            return 10
        fi
        p90="$(rb_p90_elapsed)"
        need="$(awk -v p="$p90" -v f="$RB_HEADROOM_FACTOR" 'BEGIN { printf "%d", p * f }')"
        if [[ "$remaining" -lt "$need" ]]; then
            RB_ADMIT_REASON="headroom: ${remaining}s left < p90 ${need}s"
            return 10
        fi
    fi

    # L1. A fresh reading if the probe works, otherwise the last one seen.
    if [[ "$RB_PROBE" != "off" && "$RB_PROBE_DISABLED" != "1" ]]; then
        local snap
        if snap="$(rb_probe_rate_limits)" && [[ -n "$snap" ]]; then
            rb_append_budget probe \
                "$(printf '%s' "$snap" | cut -f1)" "$(printf '%s' "$snap" | cut -f2)" \
                "$(printf '%s' "$snap" | cut -f3)" "$(printf '%s' "$snap" | cut -f4)" \
                0 "plan=$(printf '%s' "$snap" | cut -f5)"
        fi
    fi
    line="$(rb_last_budget)"
    if [[ -n "$line" ]]; then
        pct="$(printf '%s' "$line" | cut -f4)"
        reset="$(printf '%s' "$line" | cut -f5)"
        # A window that has already rolled makes its percentage meaningless.
        if [[ "$reset" =~ ^[0-9]+$ && "$reset" -le "$now" ]]; then
            pct=""
        fi
        if [[ -n "$pct" ]]; then
            if awk -v p="$pct" -v h="$RB_HARD_PCT" 'BEGIN { exit !(p >= h) }'; then
                RB_ADMIT_REASON="hard_percent $pct>=$RB_HARD_PCT"
                return 11
            fi
            if awk -v p="$pct" -v s="$RB_SOFT_PCT" 'BEGIN { exit !(p >= s) }'; then
                RB_ADMIT_REASON="soft_percent $pct>=$RB_SOFT_PCT"
                return 10
            fi
            # Percent is per-account, not per-worker: up to WORKERS jobs' worth
            # of burn can land between this reading and the next one.
            spct="$(rb_pct_per_job)"
            if awk -v p="$pct" -v w="$RB_WORKERS" -v d="$spct" -v h="$RB_HARD_PCT" \
                   'BEGIN { exit !(p + w * d >= h) }'; then
                RB_ADMIT_REASON="projected $pct+${RB_WORKERS}x${spct}>=$RB_HARD_PCT"
                return 10
            fi
        fi
    fi

    # L2.
    if [[ "$RB_SESSION_TOKENS" -gt 0 ]]; then
        tok="$(rb_run_tokens)"
        if [[ $(( tok + $(rb_p90_tokens) )) -gt "$RB_SESSION_TOKENS" ]]; then
            RB_ADMIT_REASON="token budget: $tok + p90 > $RB_SESSION_TOKENS"
            return 10
        fi
    fi

    return 0
}

# rb_parse_duration 90m|4h|5400|5400s|1d -> seconds; rc 2 on anything else.
rb_parse_duration() {
    local v="${1-}" n u
    n="${v%[smhd]}"; u="${v#$n}"
    [[ "$n" =~ ^[0-9]+$ ]] || return 2
    case "$u" in
        ''|s) printf '%s' "$n" ;;
        m)    printf '%s' $(( n * 60 )) ;;
        h)    printf '%s' $(( n * 3600 )) ;;
        d)    printf '%s' $(( n * 86400 )) ;;
        *)    return 2 ;;
    esac
    return 0
}

# What a distilled human log keeps from the JSONL. Field names verified against
# a live codex 0.152 run: item.completed carries .item.type and .item.text.
RB_JQ_DISTILL='
if .type == "thread.started" then "thread \(.thread_id // "?")"
elif .type == "turn.completed" then
  "turn completed  in=\(.usage.input_tokens // 0) cached=\(.usage.cached_input_tokens // 0) out=\((.usage.output_tokens // 0) + (.usage.reasoning_output_tokens // 0))"
elif .type == "turn.failed" then "TURN FAILED  \(.error.message // .message // "")"
elif .type == "error" then "ERROR  \(.message // .error.message // "")"
elif .type == "item.completed" then
  (.item.type // .item.item_type // "") as $k
  | if $k == "agent_message" then "agent> \(((.item.text // .item.message // "") | tostring)[0:2000])"
    elif $k == "command_execution" then "exec[\(.item.exit_code // "?")] \(((.item.command // "") | tostring)[0:200])"
    elif $k == "file_change" then "edit \(((.item.changes // [] | map(.path // tostring) | join(", ")) | tostring)[0:300])"
    else empty end
else empty end'

# rb_run_codex ID LOG JSONL -- COMMAND...
# JSONL goes straight to a file with no pipe on the critical path: pipefail is
# set, and a jq that died on one malformed line would SIGPIPE a 50-minute codex
# run. The human-readable distillation happens afterwards, from the file.
rb_run_codex() {
    local id="$1" log="$2" jsonl="$3" rc mirror_pid=""
    shift 3
    : >"$jsonl"

    if [[ "$RB_PROGRESS" == "verbose" ]]; then
        ( tail -f "$jsonl" 2>/dev/null \
            | "$RB_JQ" -r --unbuffered "$RB_JQ_DISTILL" 2>/dev/null \
            | awk -v id="$id" '{ printf "[%s] %s\n", id, $0; fflush() }' ) &
        mirror_pid=$!
    fi

    "$@" >"$jsonl" 2>>"$log"
    rc=$?

    if [[ -n "$mirror_pid" ]]; then
        sleep 1
        kill "$mirror_pid" 2>/dev/null
        wait "$mirror_pid" 2>/dev/null
    fi
    "$RB_JQ" -r "$RB_JQ_DISTILL" "$jsonl" 2>/dev/null >>"$log"
    return $rc
}

# rb_write_progress ID REASON
# Written by this script from what is on disk, never by the agent. The case it
# exists for is the interrupted one, and "interrupted" is precisely the failure
# to obtain another model turn, so a prompt that asks the agent to check out
# gracefully is dead code exactly when it is needed.
rb_write_progress() {
    local id="$1" reason="$2" f w cycles pages
    w="$RB_OUT_DIR/work"
    f="$w/${id}.progress.md"
    cycles="$(grep -c '^## Review cycle' "$w/${id}.review.md" 2>/dev/null || echo 0)"
    pages="$(grep -c '^===== PAGE ' "$w/${id}.protocol.txt" 2>/dev/null || echo 0)"
    {
        printf '# PROGRESS %s\n\n' "$id"
        printf -- '- status: INTERRUPTED\n- reason: %s\n- run: %s\n- at: %s\n\n' \
            "$reason" "$RB_RUN_ID" "$(rb_now_iso)"
        printf '## Artifacts on disk\n\n| file | bytes | mtime |\n|---|---|---|\n'
        local p
        for p in protocol.txt evidence.md draft.md review.md; do
            if [[ -f "$w/${id}.${p}" ]]; then
                printf '| work/%s.%s | %s | %s |\n' "$id" "$p" \
                    "$(wc -c <"$w/${id}.${p}" | tr -d ' ')" \
                    "$(date -u -r "$w/${id}.${p}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
            else
                printf '| work/%s.%s | - | absent |\n' "$id" "$p"
            fi
        done
        printf '\n- review cycles recorded: %s\n- protocol page markers: %s\n\n' \
            "${cycles//[^0-9]/}" "${pages//[^0-9]/}"
        if [[ -s "$w/${id}.lastmsg.txt" ]]; then
            printf '## Last agent message\n\n```\n'
            head -c 800 "$w/${id}.lastmsg.txt"
            printf '\n```\n\n'
        fi
        printf '## Next session\n\nRe-run the same command. No sap_%s.md exists, so this id is\n' "$id"
        printf 'reattempted in a fresh Codex session; the files above are reused as-is\n'
        printf 'per AGENTS.md step 6.\n'
    } >"$f" 2>/dev/null
    return 0
}

# Worker signal handler: a kill mid-job should still leave a trail.
rb_on_signal() {
    [[ -n "${RB_CURRENT_ID:-}" ]] || exit 130
    rb_append_ledger "$RB_CURRENT_ID" INTERRUPTED 0 0 0 0 0 0 "signal"
    rb_write_progress "$RB_CURRENT_ID" "signal"
    exit 130
}

# ---------------------------------------------------------------- command mode

rb_budget_report() {
    [[ -s "$RB_LEDGER" ]] || { echo "no ledger yet: $RB_LEDGER"; return 0; }
    echo "ledger : $RB_LEDGER"
    echo "rows   : $(wc -l <"$RB_LEDGER" | tr -d ' ')"
    echo
    echo "state counts (all runs):"
    awk -F'\t' '{ c[$4]++ } END { for (s in c) printf "  %-18s %d\n", s, c[s] }' "$RB_LEDGER" | sort
    echo
    printf 'p90 elapsed : %ss\n' "$(rb_p90_elapsed)"
    printf 'p90 tokens  : %s\n' "$(rb_p90_tokens)"
    [[ -e "$RB_STOP" ]] && printf 'stop-file   : PRESENT - %s\n' "$(rb_stop_reason)"
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "${1:---help}" in
        --probe)
            snap="$(rb_probe_rate_limits)" || { echo "probe unavailable" >&2; exit 1; }
            printf 'primary   %s%%  resets %s\n' "$(printf '%s' "$snap" | cut -f1)" \
                "$(date -r "$(printf '%s' "$snap" | cut -f2)" 2>/dev/null || echo '-')"
            printf 'secondary %s%%  resets %s\n' "$(printf '%s' "$snap" | cut -f3)" \
                "$(date -r "$(printf '%s' "$snap" | cut -f4)" 2>/dev/null || echo '-')"
            printf 'plan      %s\n' "$(printf '%s' "$snap" | cut -f5)"
            ;;
        --report) rb_budget_report ;;
        *) sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    esac
fi
