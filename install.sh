#!/usr/bin/env bash
# ==============================================================================
# install.sh — Universal installer for kas (k8s-ai-support)
#
# Strategy (in order):
#   1. uv found        → uv tool install  (global, Python 3.12 managed by uv)
#   2. Python 3.11-3.13 found → pip + venv  (project-local .venv, wrapper in ~/.local/bin)
#   3. Neither found   → print install instructions and exit
#
# Usage:
#   bash install.sh           # fresh install or reinstall
#   bash install.sh --update  # reinstall from current directory (pick up code changes)
#   bash install.sh --uninstall
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
MIN_PY_MINOR=11
MAX_PY_MINOR=13
PREFERRED_PY_MINOR=12

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
err()     { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
section() { echo -e "\n${BOLD}${CYAN}=== $* ===${RESET}"; }

# ── Helpers ────────────────────────────────────────────────────────────────────
check_path() {
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        warn "$BIN_DIR is not in your PATH."
        echo -e "  Add to ${YELLOW}~/.bashrc${RESET} or ${YELLOW}~/.zshrc${RESET}:"
        echo -e "    ${YELLOW}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
        echo -e "  Then reload: ${YELLOW}source ~/.bashrc${RESET}"
    fi
}

verify_kas() {
    local kas_bin
    kas_bin=$(command -v kas 2>/dev/null || echo "")
    if [[ -n "$kas_bin" ]]; then
        ok "kas is available at: $kas_bin"
        "$kas_bin" version 2>/dev/null || true
    else
        warn "kas not found in PATH yet. See PATH note above."
    fi
}

# ── Uninstall ──────────────────────────────────────────────────────────────────
do_uninstall() {
    section "Uninstalling kas"

    if command -v uv &>/dev/null; then
        uv tool uninstall k8s-ai-support 2>/dev/null && ok "Removed uv tool install" || true
    fi

    for f in "$BIN_DIR/kas" "$BIN_DIR/k8s-ai-support"; do
        [[ -f "$f" ]] && rm -f "$f" && ok "Removed $f" || true
    done

    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        ok "Removed $VENV_DIR"
    fi

    ok "Uninstall complete."
    exit 0
}

# ── Method 1: uv ──────────────────────────────────────────────────────────────
install_with_uv() {
    section "Installing with uv"
    info "uv version: $(uv --version)"

    # Ensure Python 3.12 is available under uv
    if ! uv python list --only-installed 2>/dev/null | grep -q "3\.$PREFERRED_PY_MINOR"; then
        info "Installing Python 3.$PREFERRED_PY_MINOR via uv..."
        uv python install "3.$PREFERRED_PY_MINOR"
    else
        info "Python 3.$PREFERRED_PY_MINOR already available."
    fi

    # Remove any previous install (regardless of which directory it pointed to)
    info "Removing previous install if any..."
    uv tool uninstall k8s-ai-support 2>/dev/null || true

    info "Installing k8s-ai-support from: $SCRIPT_DIR"
    uv tool install --python "3.$PREFERRED_PY_MINOR" --editable "$SCRIPT_DIR[all]"

    ok "Installed via uv."
    echo ""
    verify_kas
}

# ── Method 2: Python venv ─────────────────────────────────────────────────────
find_python() {
    local candidates=()
    for minor in $PREFERRED_PY_MINOR $(seq $MAX_PY_MINOR -1 $MIN_PY_MINOR); do
        candidates+=("python3.$minor")
    done
    candidates+=("python3" "python")

    for cmd in "${candidates[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then continue; fi
        local ver
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
        local major minor
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -eq 3 && "$minor" -ge $MIN_PY_MINOR && "$minor" -le $MAX_PY_MINOR ]]; then
            echo "$cmd"
            return 0
        fi
    done
    return 1
}

install_with_venv() {
    section "Installing with Python venv (uv not found)"

    local PYTHON
    if ! PYTHON=$(find_python); then
        return 1
    fi

    local pyver
    pyver=$("$PYTHON" --version 2>&1)
    info "Using: $PYTHON ($pyver)"

    # Create / recreate venv
    if [[ -d "$VENV_DIR" ]]; then
        info "Removing existing venv..."
        rm -rf "$VENV_DIR"
    fi
    info "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"

    info "Upgrading pip..."
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet

    info "Installing k8s-ai-support[all] (this may take a few minutes)..."
    "$VENV_DIR/bin/pip" install --editable "$SCRIPT_DIR[all]" --quiet

    # Create ~/.local/bin wrapper scripts
    mkdir -p "$BIN_DIR"
    for cmd in kas k8s-ai-support; do
        local target="$VENV_DIR/bin/$cmd"
        local wrapper="$BIN_DIR/$cmd"
        # pip editable creates entry points in venv/bin — wrap them
        cat > "$wrapper" << WRAPPER
#!/usr/bin/env bash
exec "$target" "\$@"
WRAPPER
        chmod +x "$wrapper"
        info "Created wrapper: $wrapper → $target"
    done

    ok "Installed via venv."
    echo ""
    check_path
    echo ""
    verify_kas
}

# ── No runtime found ──────────────────────────────────────────────────────────
no_runtime() {
    section "No compatible runtime found"
    err "Could not find uv or Python 3.11–3.13 on this system."
    echo ""
    echo -e "${BOLD}Install one of the following, then re-run this script:${RESET}"
    echo ""
    echo -e "  ${CYAN}Option A — uv (recommended)${RESET}"
    echo -e "  uv manages Python automatically — no separate Python install needed."
    echo ""
    echo -e "    ${YELLOW}curl -LsSf https://astral.sh/uv/install.sh | sh${RESET}"
    echo -e "    ${YELLOW}source \$HOME/.local/bin/env${RESET}   # or open a new terminal"
    echo -e "    ${YELLOW}bash install.sh${RESET}"
    echo ""
    echo -e "  ${CYAN}Option B — Python 3.12${RESET}"
    echo ""
    echo -e "    Ubuntu / Debian:  ${YELLOW}sudo apt install python3.12 python3.12-venv${RESET}"
    echo -e "    Fedora / RHEL:    ${YELLOW}sudo dnf install python3.12${RESET}"
    echo -e "    macOS (Homebrew): ${YELLOW}brew install python@3.12${RESET}"
    echo -e "    Windows (winget): ${YELLOW}winget install Python.Python.3.12${RESET}"
    echo ""
    echo -e "    Then re-run: ${YELLOW}bash install.sh${RESET}"
    exit 1
}

# ── Entry point ───────────────────────────────────────────────────────────────
section "kas (k8s-ai-support) Installer"
info "Project directory: $SCRIPT_DIR"

# Handle flags
case "${1:-}" in
    --uninstall) do_uninstall ;;
    --update)
        info "--update: reinstalling from current directory to pick up code changes."
        ;;
    "")
        ;;
    *)
        err "Unknown option: $1"
        echo "Usage: bash install.sh [--update | --uninstall]"
        exit 1
        ;;
esac

cd "$SCRIPT_DIR"

# ── Pick install method ────────────────────────────────────────────────────────
if command -v uv &>/dev/null; then
    install_with_uv
elif find_python &>/dev/null; then
    warn "uv not found — falling back to Python venv."
    warn "For a better experience, install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    install_with_venv
else
    no_runtime
fi

# ── Final summary ─────────────────────────────────────────────────────────────
section "Next steps"
echo ""
echo -e "  ${CYAN}kas --help${RESET}                              show all commands"
echo -e "  ${CYAN}kas version${RESET}                             verify install + provider"
echo -e "  ${CYAN}kas check${RESET}                               verify cluster + LLM config"
echo -e "  ${CYAN}kas \"why is my pod crashing?\"${RESET}          diagnose a live issue"
echo -e "  ${CYAN}kas \"pod failing\" -n production${RESET}         target a namespace"
echo -e "  ${CYAN}kas --interactive${RESET}                        start interactive REPL"
echo ""
echo -e "  To update after a ${CYAN}git pull${RESET}: ${YELLOW}bash install.sh --update${RESET}"
echo -e "  To uninstall:      ${YELLOW}bash install.sh --uninstall${RESET}"
echo ""
