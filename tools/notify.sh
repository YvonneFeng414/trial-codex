#!/usr/bin/env bash
#
# Notification sinks for long-running jobs: a local macOS banner and an email
# through an authenticated SMTP relay. Used two ways.
#
# As a library, which is how run_batch.sh consumes it:
#
#   . tools/notify.sh
#   rb_notify "subject" "body"                     # both sinks
#   rb_notify_run done 12 0 1 2 0 0 0 4830 ""      # batch-summary shape
#
# As a command, for anything else that takes long enough to walk away from:
#
#   make release && tools/notify.sh --subject "release built"
#   tools/notify.sh --test                         # prove the path, check the phone
#
# Run tools/notify.sh --help for the SMTP setup. Two properties are load-bearing
# and easy to break by accident:
#
#   1. No credential is ever exported. The credentials file is sourced inside a
#      subshell that dies with the call, in the parent process only, so no child
#      - and in particular no Codex session - can inherit a secret from us. The
#      RB_SMTP_TOKEN spelling is a second layer: Codex's default environment
#      policy strips *TOKEN*, and does not strip *PASS*.
#   2. Sourced, every function returns 0. A notification that fails must never
#      change the fate of the job it is reporting on.
#
# Run as a command the exit code does report delivery, since that is the whole
# job: 0 accepted (or mail deliberately off), 64 no credentials, 65 no temp
# file, otherwise curl's own status.

# Config. Every default yields to a value the caller has already set, so
# run_batch.sh's flags win and a fixture can redirect RB_ENV_FILES in a test.
RB_NOTIFY_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${RB_NOTIFY:=1}"
: "${RB_MAIL:=1}"
: "${RB_MAIL_TO_FLAG:=}"
: "${RB_ENV_FILES:=$RB_NOTIFY_HOME/.env $RB_NOTIFY_HOME/.run_batch_env $HOME/.run_batch_env}"
: "${RB_NOTIFY_LOG:=${TMPDIR:-/tmp}/rb_notify.log}"
RB_MAIL_RC=0

# rb_hms SECONDS -> 4h12m for long runs, 12m03s for short ones
rb_hms() {
    local s="$1"
    if [[ "$s" -ge 3600 ]]; then
        printf '%dh%02dm' "$((s / 3600))" "$(((s % 3600) / 60))"
    else
        printf '%dm%02ds' "$((s / 60))" "$((s % 60))"
    fi
}

# Collapse to one line and drop the two characters that break an AppleScript
# string literal or let a subject inject a mail header.
rb_oneline() {
    printf '%s' "$1" | tr '\r\n' '  ' | tr -d '"\\'
}

# rb_notify_local TITLE MESSAGE
# Best-effort: an osascript banner is attributed to "Script Editor" and is
# dropped silently when that app's notification permission is off, so the bell
# and the stderr line always fire as well.
rb_notify_local() {
    local t m
    t="$(rb_oneline "$1")"
    m="$(rb_oneline "$2")"
    if [[ "$RB_NOTIFY" == "1" ]]; then
        if command -v terminal-notifier >/dev/null 2>&1; then
            terminal-notifier -title "$t" -message "$m" >/dev/null 2>&1
        elif command -v osascript >/dev/null 2>&1; then
            osascript -e "display notification \"$m\" with title \"$t\" sound name \"Glass\"" >/dev/null 2>&1
        fi
        printf '\a' >&2
    fi
    echo "[notify] $t" >&2
    return 0
}

# rb_notify_mail SUBJECT BODY
# Sets RB_MAIL_RC; always returns 0.
rb_notify_mail() {
    local subject
    subject="$(rb_oneline "$1")"
    (
        [[ "$RB_MAIL" == "1" ]] || exit 64

        set -a
        for f in $RB_ENV_FILES; do
            [[ -f "$f" ]] && . "$f"
        done
        set +a

        # A --mail-to flag outranks the file; RB_SMTP_PASS is accepted so an
        # older credentials file keeps working, but TOKEN is the documented name.
        [[ -n "$RB_MAIL_TO_FLAG" ]] && RB_MAIL_TO="$RB_MAIL_TO_FLAG"
        : "${RB_SMTP_TOKEN:=${RB_SMTP_PASS:-}}"
        : "${RB_MAIL_FROM:=${RB_SMTP_USER:-}}"

        if [[ -z "${RB_MAIL_TO:-}" || -z "${RB_SMTP_URL:-}" \
            || -z "${RB_SMTP_USER:-}" || -z "$RB_SMTP_TOKEN" ]]; then
            echo "[mail] skipped: set RB_MAIL_TO and RB_SMTP_URL/USER/TOKEN in one of: $RB_ENV_FILES" >&2
            exit 64
        fi

        body="$(mktemp -t rb_mail)" || exit 65
        # SMTP wants CRLF; build the message with LF and convert in one pass.
        printf 'From: %s\nTo: %s\nSubject: %s\nDate: %s\nMIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n%s\n' \
            "$RB_MAIL_FROM" "$RB_MAIL_TO" "$subject" "$(date -R)" "$2" \
            | awk '{ sub(/\r$/, ""); printf "%s\r\n", $0 }' >"$body"

        # The token goes to curl on stdin, never on the command line where ps
        # would show it. --max-time keeps a hung handshake from delaying exit.
        printf 'user = "%s:%s"\n' "$RB_SMTP_USER" "$RB_SMTP_TOKEN" \
            | curl -sS --max-time 20 --ssl-reqd \
                --url "$RB_SMTP_URL" \
                --mail-from "$RB_MAIL_FROM" --mail-rcpt "$RB_MAIL_TO" \
                --upload-file "$body" -K - >>"$RB_NOTIFY_LOG" 2>&1
        rc=$?
        rm -f "$body"
        [[ "$rc" -eq 0 ]] || echo "[mail] curl rc=$rc -> $RB_NOTIFY_LOG" >&2
        exit "$rc"
    )
    RB_MAIL_RC=$?
    return 0
}

# rb_notify SUBJECT BODY [BANNER_MESSAGE]
# The banner gets its own short message because a lock screen shows far less
# than an inbox; it falls back to the subject.
rb_notify() {
    rb_notify_local "$1" "${3:-$1}"
    rb_notify_mail "$1" "$2"
    return 0
}

# rb_notify_run STATE OK SKIP SCAN FAIL LIMIT DEFER LEFT ELAPSED REASON
# STATE is the word in the subject: "done" now, "drained" once the budget gate
# lands. Everything the eye needs is in the subject, so it reads from a lock
# screen. The body is counts and paths only - no protocol or SAP text leaves
# the machine. RB_RUN_ID and RB_STATUS are optional context: the run id falls
# back to a placeholder, and the status line is dropped when there is no file.
rb_notify_run() {
    local state="$1" ok="$2" skip="$3" scan="$4" fail="$5" limit="$6" defer="$7" left="$8"
    shift 8
    local elapsed="$1" reason="$2"
    local subject body

    subject="run_batch $state: $ok ok / $fail fail / $limit limit / $left left"
    body="run ${RB_RUN_ID:-(unnamed)}   elapsed $(rb_hms "$elapsed")
ok $ok · skipped $skip · failed $fail · limit-hit $limit · deferred $defer · unsupported-scan $scan"
    [[ -n "$reason" ]] && body="$body
stop reason: $reason"
    [[ -n "${RB_STATUS:-}" ]] && body="$body
status: $RB_STATUS"
    body="$body
resume: re-run the same command; completed outputs are skipped"

    rb_notify "$subject" "$body" "$ok ok · $fail failed · $left left · $(rb_hms "$elapsed")"
    return 0
}

# Explain RB_MAIL_RC on stdout. Shared by both command-line front ends.
rb_notify_report() {
    case "$RB_MAIL_RC" in
        0)  echo "mail: accepted for delivery (accepted is not delivered - check the inbox, and how fast the phone alerts)" ;;
        64) echo "mail: not sent (disabled, or credentials missing)" ;;
        65) echo "mail: not sent (could not create the temporary message file)" ;;
        *)  echo "mail: curl rc=$RB_MAIL_RC, see $RB_NOTIFY_LOG" ;;
    esac
    return 0
}

# A dummy run summary through both sinks, so the alert path can be proven
# before a multi-hour run depends on it.
rb_notify_test() {
    echo "notify test${RB_RUN_ID:+: run $RB_RUN_ID}"
    echo "banner : $([[ "$RB_NOTIFY" == "1" ]] && echo enabled || echo 'disabled (--no-notify)')"
    echo "mail   : $([[ "$RB_MAIL" == "1" ]] && echo "enabled, log $RB_NOTIFY_LOG" || echo 'disabled (--no-mail)')"
    echo
    rb_notify_run notify-test 214 0 6 3 1 1116 1116 15120 "dummy summary; no batch was run"
    rb_notify_report
    return 0
}

# The setup instructions, reused verbatim by run_batch.sh --help so there is
# one copy of them.
rb_notify_usage() {
    cat <<'EOF'
Notification is delivered by tools/notify.sh: a local banner, and an email if
one is configured. The banner needs nothing; the email needs these in ./.env,
./.run_batch_env, or ~/.run_batch_env (chmod 600, git-ignored, never exported):

  RB_MAIL_TO      recipient
  RB_MAIL_FROM    envelope sender (default: RB_SMTP_USER)
  RB_SMTP_URL     e.g. smtps://smtp.gmail.com:465
  RB_SMTP_USER    SMTP account
  RB_SMTP_TOKEN   SMTP password or app password

Use the name RB_SMTP_TOKEN, not RB_SMTP_PASS: Codex's default environment
policy strips *TOKEN* from the environment of commands the agent runs, so the
spelling buys a second layer of protection for free. Use a dedicated sender
account - a Gmail app password also grants full mailbox read.
EOF
}

rb_notify_cli_usage() {
    cat <<'EOF'
Send a notification: a local macOS banner, and an email if one is configured.
For anything that takes long enough to walk away from.

  make release && tools/notify.sh --subject "release built"
  tools/notify.sh --test            # prove the path, then watch the phone

Exit code: 0 accepted for delivery (or --no-mail, where a banner alone is the
whole request), 64 no credentials, 65 no temporary file, else curl's status.

EOF
    rb_notify_usage
    cat <<'EOF'

Options:
  --subject TEXT  subject line and banner title (default: "notification")
  --body TEXT     message body (default: the subject)
  --mail-to ADDR  recipient, overriding $RB_MAIL_TO
  --no-mail       local banner only
  --no-notify     email only
  --test          send a dummy run summary and report what happened
  -h, --help      show this message
EOF
}

# ---------------------------------------------------------------------------
# Command-line front end; skipped entirely when sourced.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -uo pipefail

    rb_cli_subject=""
    rb_cli_body=""
    rb_cli_test=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --subject)  [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; rb_cli_subject="$2"; shift 2 ;;
            --body)     [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; rb_cli_body="$2"; shift 2 ;;
            --mail-to)  [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 2; }; RB_MAIL_TO_FLAG="$2"; shift 2 ;;
            --no-mail)  RB_MAIL=0; shift ;;
            --no-notify) RB_NOTIFY=0; shift ;;
            --test)     rb_cli_test=1; shift ;;
            -h|--help)  rb_notify_cli_usage; exit 0 ;;
            *) echo "unknown option: $1" >&2; rb_notify_cli_usage >&2; exit 2 ;;
        esac
    done

    if [[ "$rb_cli_test" -eq 1 ]]; then
        rb_notify_test
    else
        [[ -n "$rb_cli_subject" ]] || rb_cli_subject="notification"
        rb_notify "$rb_cli_subject" "${rb_cli_body:-$rb_cli_subject}"
        rb_notify_report
    fi
    # --no-mail asked for a banner and got one: that is success, not RB_MAIL_RC's
    # "disabled or not configured". Only an attempted delivery reports its fate.
    [[ "$RB_MAIL" == "1" ]] || exit 0
    exit "$RB_MAIL_RC"
fi
