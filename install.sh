#!/usr/bin/env bash

set -euo pipefail

default_repository_url="https://github.com/Kie-Chi/envY.git"
repository_url="${ENVY_REPOSITORY_URL:-$default_repository_url}"
repository_url_explicit=0
[ -n "${ENVY_REPOSITORY_URL:-}" ] && repository_url_explicit=1
branch="${ENVY_BRANCH:-master}"
target="${ENVY_ROOT:-${ENVY_DOTFILES:-${DOTFILES_DIR:-${HOME:?HOME is not set}/.envy}}}"
mirror="${ENVY_MIRROR:-china}"
git_mirror_url="${ENVY_GIT_MIRROR_URL:-https://gh-proxy.com/}"
run_setup=1
force=0
reset_secrets=0
temporary_dir=""
backup_target=""

usage() {
    cat <<'EOF'
Usage: install.sh [options]

Bootstrap the envY repository, then run its setup.sh.

Options:
  --repo URL       Git repository URL
  --branch NAME    Branch to clone (default: master)
  --target PATH    Checkout path (default: $HOME/.envy)
  --mirror MODE    Bootstrap mirror: china or upstream (default: china)
  --no-setup       Clone only; do not run setup.sh
  -f, --force      Re-download and replace the target checkout
  --reset-secrets  Start setup with empty secrets and replace secrets.yaml
  -h, --help       Show this help

Environment equivalents:
  ENVY_REPOSITORY_URL, ENVY_BRANCH, ENVY_ROOT, ENVY_MIRROR
  ENVY_DOTFILES and DOTFILES_DIR are deprecated aliases for ENVY_ROOT.
  ENVY_GIT_MIRROR_URL overrides the China GitHub proxy (default: gh-proxy.com).
  ENVY_NIX_INSTALLER_URL overrides the Nix installer endpoint.
EOF
}

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

# shellcheck disable=SC2329  # Invoked indirectly by trap.
cleanup() {
    if [ -n "$backup_target" ] && [ -e "$backup_target" ] && [ ! -e "$target" ]; then
        mv "$backup_target" "$target" || true
    fi
    if [ -n "$temporary_dir" ] && [ -d "$temporary_dir" ]; then
        rm -rf "$temporary_dir"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo)
            [ "$#" -ge 2 ] || fail "--repo requires a value"
            repository_url="$2"
            repository_url_explicit=1
            shift 2
            ;;
        --branch)
            [ "$#" -ge 2 ] || fail "--branch requires a value"
            branch="$2"
            shift 2
            ;;
        --target)
            [ "$#" -ge 2 ] || fail "--target requires a value"
            target="$2"
            shift 2
            ;;
        --mirror)
            [ "$#" -ge 2 ] || fail "--mirror requires a value"
            mirror="$2"
            shift 2
            ;;
        --no-setup)
            run_setup=0
            shift
            ;;
        -f|--force)
            force=1
            shift
            ;;
        --reset-secrets)
            reset_secrets=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

[ -n "$repository_url" ] || fail "repository URL cannot be empty"
[ -n "$branch" ] || fail "branch cannot be empty"
[ -n "$target" ] || fail "target cannot be empty"
[ -n "$git_mirror_url" ] || fail "Git mirror URL cannot be empty"

case "$mirror" in
    china|upstream)
        ;;
    *)
        fail "--mirror must be 'china' or 'upstream'"
        ;;
esac

case "$target" in
    /|"$HOME")
        fail "refusing to use a broad target path: $target"
        ;;
esac

command -v git >/dev/null 2>&1 || fail "git is required before bootstrapping"

clone_repository() {
    local clone_url
    local normalized_mirror_url
    local clone_index=0
    local clone_count
    local -a clone_urls=()

    # An explicitly supplied repository is already a trust decision. Do not
    # rewrite it through a third-party proxy. The default GitHub URL gets a
    # domestic proxy in China mode, followed by the upstream fallback.
    if [ "$repository_url_explicit" -eq 1 ] || [ "$mirror" = "upstream" ] \
        || [[ "$repository_url" != https://github.com/* ]]; then
        clone_urls=("$repository_url")
    else
        normalized_mirror_url="${git_mirror_url%/}/"
        clone_urls=("${normalized_mirror_url}${repository_url}" "$repository_url")
    fi

    clone_count="${#clone_urls[@]}"
    for clone_url in "${clone_urls[@]}"; do
        printf 'Cloning %s (%s)...\n' "$clone_url" "$branch"
        if git -c http.connectTimeout=15 \
            -c http.lowSpeedLimit=1024 \
            -c http.lowSpeedTime=30 \
            clone --quiet --single-branch --branch "$branch" \
            "$clone_url" "$temporary_dir/repository"; then
            return 0
        fi

        # A failed clone may leave a partial destination behind. It is inside
        # our private temporary directory, so remove only that partial clone
        # before trying the next endpoint.
        rm -rf "$temporary_dir/repository"
        if [ "$((clone_index + 1))" -lt "$clone_count" ]; then
            printf 'Clone endpoint failed; trying the next endpoint...\n' >&2
        fi
        clone_index=$((clone_index + 1))
    done

    return 1
}

parent_dir="$(dirname "$target")"
mkdir -p "$parent_dir"

if [ -d "$target/.git" ] && [ "$force" -eq 0 ]; then
    printf 'Using existing checkout: %s\n' "$target"
elif [ -e "$target" ] && [ "$force" -eq 0 ]; then
    fail "target exists but is not a Git checkout: $target"
else
    # Keep the temporary clone beside the target so replacement is a same-file
    # system rename. A failed clone therefore leaves the existing checkout
    # untouched, and a successful clone can be swapped in without a reboot.
    temporary_dir="$(mktemp -d "$parent_dir/.envy-bootstrap.XXXXXX")"
    trap cleanup EXIT HUP INT TERM

    clone_repository || fail "unable to clone repository; set ENVY_REPOSITORY_URL to a reachable trusted Git remote"
    [ -f "$temporary_dir/repository/setup.sh" ] \
        || fail "setup.sh is missing from the cloned repository"

    if [ -e "$target" ]; then
        printf 'Replacing existing checkout: %s\n' "$target"
        backup_target="$temporary_dir/previous-target"
        mv "$target" "$backup_target" \
            || fail "unable to move the existing target out of the way: $target"
    fi
    if ! mv "$temporary_dir/repository" "$target"; then
        if [ -n "$backup_target" ] && [ -e "$backup_target" ]; then
            mv "$backup_target" "$target" || true
        fi
        fail "unable to install the cloned repository at: $target"
    fi
    if [ -n "$backup_target" ] && [ -e "$backup_target" ]; then
        rm -rf "$backup_target"
    fi
    backup_target=""
    rmdir "$temporary_dir"
    temporary_dir=""
    trap - EXIT HUP INT TERM
fi

[ -f "$target/setup.sh" ] || fail "setup.sh is missing from checkout: $target"

if [ "$run_setup" -eq 0 ]; then
    printf 'Checkout ready. Run: bash %s/setup.sh\n' "$target"
    exit 0
fi

export ENVY_ROOT="$target"
export ENVY_MIRROR="$mirror"
if [ "$reset_secrets" -eq 1 ]; then
    export ENVY_SETUP_RESET_SECRETS=1
fi
if [ -t 0 ]; then
    exec "$BASH" "$target/setup.sh"
fi

# `curl ... | bash` occupies stdin with the script body. Reattach the
# interactive setup to the controlling terminal after the clone completes.
if tty -s </dev/tty 2>/dev/null; then
    exec "$BASH" "$target/setup.sh" </dev/tty
fi

fail "interactive setup requires a terminal; rerun with --no-setup to clone only"
