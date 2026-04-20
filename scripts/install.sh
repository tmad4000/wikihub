#!/usr/bin/env sh
# wikihub-cli installer — idempotent. Usage:
#   curl -fsSL https://wikihub.md/install.sh | sh
#
# Installs `wikihub-cli` via pipx (preferred) or pip --user. Prints the
# resolved binary path so the user can `export PATH` if needed.
set -eu

log() { printf '\033[1;33m→\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*" >&2; }

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        warn "missing required command: $1"
        return 1
    fi
}

PIPX="$(command -v pipx || true)"
PIP="$(command -v pip3 || command -v pip || true)"

if [ -n "$PIPX" ]; then
    log "using pipx ($PIPX)"
    INSTALLER="pipx install --force wikihub-cli"
elif [ -n "$PIP" ]; then
    log "using $PIP (pipx not found — strongly prefer pipx)"
    INSTALLER="$PIP install --user --upgrade wikihub-cli"
else
    warn "neither pipx nor pip found. Install one first:"
    warn "  macOS:   brew install pipx && pipx ensurepath"
    warn "  Debian:  sudo apt install pipx && pipx ensurepath"
    warn "  Windows: python -m pip install --user pipx && python -m pipx ensurepath"
    exit 1
fi

log "installing wikihub-cli..."
sh -c "$INSTALLER" || {
    warn "install failed"
    exit 1
}

BIN="$(command -v wikihub || true)"
if [ -n "$BIN" ]; then
    log "installed: $BIN"
    log "run: wikihub signup --username <you>"
else
    warn "install finished but \`wikihub\` not on PATH."
    warn "if you used pipx: run \`pipx ensurepath\` and open a new shell."
    warn "if you used pip --user: add \`~/.local/bin\` to your PATH."
fi
