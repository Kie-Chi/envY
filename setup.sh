#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIRES_SCRIPT="$BASE_DIR/requires.sh"
MIRROR_ENV_SCRIPT="$BASE_DIR/resources/scripts/mirror-env.sh"
NIX_TRUST_SCRIPT="$BASE_DIR/resources/scripts/nix-trust.sh"
NIX_PROFILE_SCRIPT='/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh'
NIX_BIN_DIR='/nix/var/nix/profiles/default/bin'
mirror="${ENVY_MIRROR:-china}"
export ENVY_ROOT="$BASE_DIR"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mirror)
            [ "$#" -ge 2 ] || { echo "[ERROR] --mirror requires a value" >&2; exit 2; }
            mirror="$2"
            shift 2
            ;;
        *)
            echo "[ERROR] unknown option: $1" >&2
            exit 2
            ;;
    esac
done

case "$mirror" in
    china|upstream)
        export ENVY_MIRROR="$mirror"
        ;;
    *)
        echo "[ERROR] --mirror must be 'china' or 'upstream'" >&2
        exit 2
        ;;
esac

load_nix_path() {
    if [ -e "$NIX_PROFILE_SCRIPT" ]; then
        # shellcheck source=/dev/null
        . "$NIX_PROFILE_SCRIPT"
    fi

    # The installer runs as a child process, so its PATH changes cannot be
    # inherited. Restore the stable profile locations explicitly as well.
    if [ -d "$NIX_BIN_DIR" ]; then
        PATH="$NIX_BIN_DIR:${PATH:-}"
    fi
    if [ -n "${HOME:-}" ] && [ -d "$HOME/.nix-profile/bin" ]; then
        PATH="$HOME/.nix-profile/bin:${PATH:-}"
    fi
    export PATH
}

# Bootstrap-time mirrors make the first nix develop usable before a machine
# module has been evaluated and applied.
# shellcheck source=resources/scripts/mirror-env.sh
. "$MIRROR_ENV_SCRIPT"

# ==========================================
# STEP 1: Ensure Nix is available
# ==========================================

# Source Nix profile if not already in PATH (handles "nix installed but PATH not set" scenario)
if ! command -v nix >/dev/null 2>&1; then
    load_nix_path
fi

if ! command -v nix >/dev/null 2>&1; then
    echo "[INFO] Nix not found. Installing..."
    if [ -f "$REQUIRES_SCRIPT" ]; then
        chmod +x "$REQUIRES_SCRIPT"
        "$REQUIRES_SCRIPT"
        # Source Nix profile again after fresh install (subprocess env doesn't propagate to parent)
        load_nix_path
    else
        echo "[ERROR] requires.sh not found and Nix is not installed!"
        exit 1
    fi
fi

command -v nix >/dev/null 2>&1 || {
    echo "[ERROR] Nix was installed but is not available on PATH. Expected: $NIX_BIN_DIR/nix" >&2
    exit 1
}

if [ "$(uname -s)" = "Linux" ] || [ "$(uname -s)" = "Darwin" ]; then
    [ -r "$NIX_TRUST_SCRIPT" ] || {
        echo "[ERROR] Nix trust helper is missing: $NIX_TRUST_SCRIPT" >&2
        exit 1
    }
    bash "$NIX_TRUST_SCRIPT" repair --mode "$ENVY_MIRROR" --user "$(id -un)"
fi

# ==========================================
# STEP 2: Enter the minimal setup runtime
# ==========================================

if [ "${ENVY_DEV_SHELL:-0}" = "1" ] \
    || { python3 -c 'import typer, rich, prompt_toolkit, yaml' 2>/dev/null \
        && command -v sops >/dev/null 2>&1 \
        && command -v age >/dev/null 2>&1 \
        && command -v ssh-to-age >/dev/null 2>&1; }; then
    # Applied envY packages and the development shell already provide runtime dependencies.
    echo "[INFO] Using the available envY setup runtime..."
    export PYTHONPATH="$BASE_DIR/resources/scripts:${PYTHONPATH:-}"
    exec python3 "$BASE_DIR/setup.py"
else
    echo "[INFO] Preparing the envY setup runtime..."
    exec nix run "path:$BASE_DIR#setup"
fi
