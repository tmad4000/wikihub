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

# PyPI target (preferred). If the PyPI package isn't published yet,
# the installer falls back to pip-install-from-git so the one-liner
# still works during the interim.
PYPI_PKG="wikihub-cli"
GIT_SPEC="git+https://github.com/tmad4000/wikihub.git#subdirectory=cli"

install_via() {
    # $1 = tool, $2 = args
    sh -c "$1 $2"
}

if [ -z "$PIPX" ] && [ -z "$PIP" ]; then
    warn "neither pipx nor pip found. Install one first:"
    warn "  macOS:   brew install pipx && pipx ensurepath"
    warn "  Debian:  sudo apt install pipx && pipx ensurepath"
    warn "  Windows: python -m pip install --user pipx && python -m pipx ensurepath"
    exit 1
fi

TOOL=""
if [ -n "$PIPX" ]; then
    TOOL="pipx"
    log "using pipx ($PIPX)"
else
    TOOL="$PIP"
    log "using $TOOL (pipx not found — strongly prefer pipx)"
fi

# Try PyPI first; fall back to git if the package isn't published.
log "installing $PYPI_PKG..."
if [ "$TOOL" = "pipx" ]; then
    if ! pipx install --force "$PYPI_PKG" 2>/dev/null; then
        log "PyPI package not found — falling back to git install"
        pipx install --force "$GIT_SPEC" || { warn "install failed"; exit 1; }
    fi
else
    if ! "$TOOL" install --user --upgrade "$PYPI_PKG" 2>/dev/null; then
        log "PyPI package not found — falling back to git install"
        "$TOOL" install --user --upgrade "$GIT_SPEC" || { warn "install failed"; exit 1; }
    fi
fi

BIN="$(command -v wikihub || true)"
if [ -n "$BIN" ]; then
    log "installed: $BIN"
    log "run: wikihub signup --username <you>"
else
    warn "install finished but \`wikihub\` not on PATH."
    warn "if you used pipx: run \`pipx ensurepath\` and open a new shell."
    warn "if you used pip --user: add \`~/.local/bin\` to your PATH."
fi
