#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_CODEX_HOME="$REPO_DIR/.codex-home"
PROJECT_AUTH="$PROJECT_CODEX_HOME/auth.json"
USER_CODEX_AUTH="$HOME/.codex/auth.json"
MODE="login"
FORCE=0

usage() {
    cat <<'EOF'
Usage: tools/init_codex_home.sh [--login | --copy-current] [--force]

Initialize this repository's private .codex-home directory.

  --login         sign in interactively and write a new auth.json (default)
  --copy-current  copy ~/.codex/auth.json instead of signing in
  --force         replace an existing project auth.json
  -h, --help      show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --login) MODE="login" ;;
        --copy-current) MODE="copy" ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

command -v codex >/dev/null 2>&1 || {
    echo "codex executable not found" >&2
    exit 1
}

umask 077
mkdir -p "$PROJECT_CODEX_HOME"
chmod 700 "$PROJECT_CODEX_HOME"

if [[ ! -e "$PROJECT_CODEX_HOME/config.toml" ]]; then
    printf '%s\n' 'cli_auth_credentials_store = "file"' \
        >"$PROJECT_CODEX_HOME/config.toml"
fi
chmod 600 "$PROJECT_CODEX_HOME/config.toml"

if [[ -e "$PROJECT_AUTH" && "$FORCE" -ne 1 ]]; then
    echo "project auth already exists: $PROJECT_AUTH" >&2
    echo "re-run with --force to replace it" >&2
    exit 1
fi

if [[ "$MODE" == "copy" ]]; then
    [[ -f "$USER_CODEX_AUTH" ]] || {
        echo "current file-backed login not found: $USER_CODEX_AUTH" >&2
        echo "use --login instead" >&2
        exit 1
    }
    install -m 600 "$USER_CODEX_AUTH" "$PROJECT_AUTH"
else
    export CODEX_HOME="$PROJECT_CODEX_HOME"
    if [[ -e "$PROJECT_AUTH" ]]; then
        codex logout
    fi
    codex login
fi

[[ -f "$PROJECT_AUTH" ]] || {
    echo "Codex did not create $PROJECT_AUTH" >&2
    exit 1
}
chmod 600 "$PROJECT_AUTH"

echo "initialized project Codex home: $PROJECT_CODEX_HOME"
CODEX_HOME="$PROJECT_CODEX_HOME" codex login status
