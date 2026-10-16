#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIRES_SCRIPT="$BASE_DIR/requires.sh"
MIRROR_ENV_SCRIPT="$BASE_DIR/resources/scripts/mirror-env.sh"
NIX_TRUST_SCRIPT="$BASE_DIR/resources/scripts/nix-trust.sh"
FLAKE_SOURCE_MIRROR_SCRIPT="$BASE_DIR/resources/scripts/flake-source-mirror.sh"
NIX_PROFILE_SCRIPT='/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh'
NIX_BIN_DIR='/nix/var/nix/profiles/default/bin'
mirror="${ENVY_MIRROR:-china}"
export ENVY_ROOT="$BASE_DIR"

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat <<'EOF'
Usage: setup.sh [--mirror china|upstream] [--reset-secrets]

Options:
  --mirror MODE    Select bootstrap mirrors
  --reset-secrets  Start setup with empty secrets and replace secrets.yaml
  -h, --help       Show this help
EOF
            exit 0
            ;;
        --reset-secrets)
            export ENVY_SETUP_RESET_SECRETS=1
            shift
            ;;
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
# The trust helper below must be the source of bootstrap substituters. Passing
# them through NIX_CONFIG makes them client overrides, which an untrusted Nix
# daemon is required to reject even while its config is being reloaded.
export ENVY_SETUP_BOOTSTRAP=1
# shellcheck source=resources/scripts/mirror-env.sh
. "$MIRROR_ENV_SCRIPT"

# ==========================================
# STEP 1: Ensure Nix is available
# ==========================================

# Source Nix profile only when nix is not already available. This preserves a
# caller-provided nix path while still handling installs that need the profile.
if ! command -v nix >/dev/null 2>&1; then
    load_nix_path
fi

if [ -f "$REQUIRES_SCRIPT" ]; then
    chmod +x "$REQUIRES_SCRIPT"
    # requires.sh is idempotent. Run it on every setup so a machine with Nix
    # but without Homebrew is repaired before the first Darwin activation.
    "$REQUIRES_SCRIPT"
    # Source profiles and Homebrew prefixes again because installers run as
    # child processes and cannot update this shell's environment.
    if ! command -v nix >/dev/null 2>&1; then
        load_nix_path
    fi
    if [ "$(uname -s)" = "Darwin" ]; then
        if [ -d "/opt/homebrew/bin" ]; then
            PATH="/opt/homebrew/bin:${PATH:-}"
        fi
        if [ -d "/usr/local/bin" ]; then
            PATH="/usr/local/bin:${PATH:-}"
        fi
        export PATH
    fi
else
    echo "[ERROR] requires.sh not found; cannot verify Nix/Homebrew prerequisites!" >&2
    exit 1
fi

command -v nix >/dev/null 2>&1 || {
    echo "[ERROR] Nix is not available on PATH. Expected: $NIX_BIN_DIR/nix" >&2
    exit 1
}

if [ "$(uname -s)" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || {
        echo "[ERROR] Homebrew is not available on PATH after prerequisite setup." >&2
        exit 1
    }
fi

if [ "$(uname -s)" = "Linux" ] || [ "$(uname -s)" = "Darwin" ]; then
    [ -r "$NIX_TRUST_SCRIPT" ] || {
        echo "[ERROR] Nix trust helper is missing: $NIX_TRUST_SCRIPT" >&2
        exit 1
    }
    bash "$NIX_TRUST_SCRIPT" repair --mode "$ENVY_MIRROR" --user "$(id -un)" --reload
fi

# Prime locked Flake sources through the selected mirror before the first
# `nix run` evaluates the setup flake. The helper is deliberately optional for
# older checkouts and falls back to Nix's locked upstream URLs on failure.
if [ -r "$FLAKE_SOURCE_MIRROR_SCRIPT" ]; then
    bash "$FLAKE_SOURCE_MIRROR_SCRIPT" "$BASE_DIR" prime || true
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
