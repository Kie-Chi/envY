#!/usr/bin/env bash

# Bootstrap cannot evaluate the machine module yet. Keep these values aligned
# with modules/mirrors/catalog.nix until the declarative configuration applies.
envy_mirror_mode="${ENVY_MIRROR:-china}"

case "$envy_mirror_mode" in
    china|upstream)
        ;;
    *)
        printf 'error: unsupported ENVY_MIRROR value: %s\n' "$envy_mirror_mode" >&2
        if [ "${BASH_SOURCE[0]}" = "$0" ]; then
            exit 1
        fi
        return 1
        ;;
esac

export ENVY_MIRROR="$envy_mirror_mode"

if [ "${ENVY_MIRROR_ENV_APPLIED:-}" != "$envy_mirror_mode" ]; then
    # Official Nix installs leave these disabled. Both bootstrap entry points
    # and setup's child processes need them before the first flake evaluation.
    # Use extra- to preserve any experimental features already configured.
    envy_nix_config='extra-experimental-features = nix-command flakes
fallback = true
connect-timeout = 5'
    # setup.sh sets ENVY_SETUP_BOOTSTRAP while it repairs the daemon trust
    # block. Keep the first nix run on the daemon's own trusted substituters;
    # passing these URLs through NIX_CONFIG would be an untrusted client
    # override during daemon reload. Normal shells and post-apply commands
    # retain the temporary endpoint selection below.
    if [ "${ENVY_SETUP_BOOTSTRAP:-0}" != "1" ]; then
        envy_nix_config="extra-substituters = https://cache.thalheim.io
$envy_nix_config"
        if [ "$envy_mirror_mode" = "china" ]; then
            envy_nix_config="substituters = https://mirrors.ustc.edu.cn/nix-channels/store https://cache.nixos.org/
$envy_nix_config"
        fi
    fi
    if [ "$envy_mirror_mode" = "china" ]; then
        export npm_config_registry="https://registry.npmmirror.com"
        export PIP_INDEX_URL="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
        export UV_DEFAULT_INDEX="$PIP_INDEX_URL"
        export GOPROXY="https://goproxy.cn,direct"
        export RUSTUP_DIST_SERVER="https://rsproxy.cn"
        export RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"
        export CARGO_REGISTRIES_CRATES_IO_INDEX="sparse+https://rsproxy.cn/index/"
        export CARGO_REGISTRIES_CRATES_IO_PROTOCOL="sparse"
    fi
    if [ -n "${NIX_CONFIG:-}" ]; then
        NIX_CONFIG="$NIX_CONFIG
$envy_nix_config"
    else
        NIX_CONFIG="$envy_nix_config"
    fi
    export NIX_CONFIG
    export ENVY_MIRROR_ENV_APPLIED="$envy_mirror_mode"
fi

unset envy_mirror_mode envy_nix_config
