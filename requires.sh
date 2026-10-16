#!/usr/bin/env bash

# ==============================================================================
# requires.sh - Install the system prerequisites used by envY
#
# Nix and Homebrew are installed only when missing. Other dependencies (jq,
# sops, age, Python, etc.) are provided by the devShell defined in flake.nix.
# ==============================================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

msg_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
msg_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
msg_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
msg_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

load_homebrew_path() {
    # Homebrew does not guarantee that a fresh install can update the parent
    # shell's startup files before setup continues. Make both supported macOS
    # prefixes available in this process and in callers that repeat the check.
    # ENVY_HOMEBREW_BIN_DIRS is also useful for isolated bootstrap tests.
    local homebrew_bin_dirs="${ENVY_HOMEBREW_BIN_DIRS:-/opt/homebrew/bin:/usr/local/bin}"
    local homebrew_bin
    local previous_ifs="$IFS"
    IFS=:
    for homebrew_bin in $homebrew_bin_dirs; do
        if [ -d "$homebrew_bin" ]; then
            PATH="$homebrew_bin:${PATH:-}"
        fi
    done
    IFS="$previous_ifs"
    export PATH
}

install_nix() {
    if command_exists nix; then
        msg_success "Nix is already installed."
        return 0
    fi

    msg_info "Nix not found. Installing Nix package manager..."
    read -r -p "Press Enter to continue, or Ctrl+C to cancel." </dev/tty

    command_exists curl || msg_error "curl is required to install Nix"

    local nix_installer_url
    local nix_installer_args
    local -a installer_args=()
    if [ -n "${ENVY_NIX_INSTALLER_URL:-}" ]; then
        # Keep the historical Determinate invocation for explicitly supplied
        # installer endpoints. A standard Nix installer can opt into its
        # daemon mode with ENVY_NIX_INSTALLER_ARGS.
        nix_installer_url="$ENVY_NIX_INSTALLER_URL"
        nix_installer_args="${ENVY_NIX_INSTALLER_ARGS:-install}"
    elif [ "${ENVY_MIRROR:-china}" = "china" ]; then
        # TUNA mirrors the official Nix binary installer and its tarballs. The
        # script verifies the tarball hash before running the embedded install.
        nix_installer_url="https://mirrors.tuna.tsinghua.edu.cn/nix/latest/install"
        nix_installer_args="${ENVY_NIX_INSTALLER_ARGS:---daemon}"
    else
        nix_installer_url="https://install.determinate.systems/nix"
        nix_installer_args="${ENVY_NIX_INSTALLER_ARGS:-install}"
    fi

    # Split only the explicitly documented argument string; this avoids eval
    # while still allowing flags such as `--daemon --no-channel`. Word
    # splitting is intentional here and is scoped to installer arguments.
    read -r -a installer_args <<< "$nix_installer_args"
    curl --proto '=https' --tlsv1.2 -sSf -L "$nix_installer_url" \
        | sh -s -- "${installer_args[@]}"

    msg_success "Nix installation complete."
    msg_warn "You may need to re-login or restart your shell for Nix to be available."

    # Source Nix profile in this process so subsequent function calls can use nix
    if [ -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh' ]; then
        # shellcheck source=/dev/null
        . '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh'
    fi
    if [ -d '/nix/var/nix/profiles/default/bin' ]; then
        PATH="/nix/var/nix/profiles/default/bin:${PATH:-}"
    fi
    if [ -n "${HOME:-}" ] && [ -d "$HOME/.nix-profile/bin" ]; then
        PATH="$HOME/.nix-profile/bin:${PATH:-}"
    fi
    export PATH
    command_exists nix || msg_error "Nix was installed but is not available on PATH"
}

install_homebrew() {
    if [ "$(uname -s)" != "Darwin" ]; then
        return 0
    fi

    local mirror_env_script
    mirror_env_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/resources/scripts/mirror-env.sh"
    [ -r "$mirror_env_script" ] || msg_error "Homebrew mirror environment is missing: $mirror_env_script"
    # shellcheck source=resources/scripts/mirror-env.sh
    . "$mirror_env_script"

    load_homebrew_path
    if command_exists brew; then
        brew --version >/dev/null 2>&1 \
            || msg_error "Homebrew was found but could not run"
        msg_success "Homebrew is already installed."
        return 0
    fi

    command_exists curl || msg_error "curl is required to install Homebrew"
    local homebrew_installer_url
    local upstream_installer_url="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    local installer_url_explicit=0
    if [ -n "${ENVY_HOMEBREW_INSTALL_URL:-}" ]; then
        homebrew_installer_url="$ENVY_HOMEBREW_INSTALL_URL"
        installer_url_explicit=1
    elif [ "$ENVY_MIRROR" = "china" ]; then
        homebrew_installer_url="${ENVY_BOOTSTRAP_HOMEBREW_INSTALL_URL:-https://mirrors.ustc.edu.cn/misc/brew-install.sh}"
    else
        homebrew_installer_url="${ENVY_BOOTSTRAP_HOMEBREW_INSTALL_URL:-$upstream_installer_url}"
    fi
    msg_info "Homebrew not found. Installing Homebrew (mirror: $ENVY_MIRROR)..."
    (
        # Download completely before execution. Keep stdin attached to the
        # terminal so the official installer can confirm and request sudo.
        homebrew_installer="$(mktemp "${TMPDIR:-/tmp}/envy-homebrew.XXXXXX")"
        trap 'rm -f "$homebrew_installer"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' HUP TERM
        if ! curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
            --connect-timeout 15 --max-time 120 \
            "$homebrew_installer_url" -o "$homebrew_installer"; then
            if [ "$installer_url_explicit" -eq 1 ] || [ "$ENVY_MIRROR" != "china" ]; then
                msg_error "Unable to download the Homebrew installer"
            fi
            msg_warn "USTC installer download failed; trying the official installer."
            curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
                --connect-timeout 15 --max-time 120 \
                "$upstream_installer_url" -o "$homebrew_installer" \
                || msg_error "Unable to download the Homebrew installer"
        fi
        /bin/bash "$homebrew_installer" \
            || msg_error "Homebrew installation failed; setup cannot continue"
    )

    load_homebrew_path
    if ! command_exists brew; then
        msg_error "Homebrew installation completed but brew is not available on PATH"
    fi
    brew --version >/dev/null 2>&1 \
        || msg_error "Homebrew was installed but could not run"
    msg_success "Homebrew installation complete."
}

main() {
    msg_info "Checking prerequisites..."
    install_nix
    install_homebrew
    msg_success "Prerequisites ready!"
}

main
