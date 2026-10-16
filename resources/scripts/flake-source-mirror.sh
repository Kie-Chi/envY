#!/usr/bin/env bash

# Prime Nix's store with locked Flake sources fetched from the selected mirror.
# Nix reuses a source already present under the locked narHash, so this keeps
# flake.nix and flake.lock unchanged while avoiding the original GitHub URL.

set -euo pipefail

source_mirror_root="${1:-${ENVY_ROOT:-$(pwd)}}"
source_mirror_action="${2:-prime}"

if [ "$source_mirror_action" != "prime" ]; then
    printf 'error: unsupported flake source mirror action: %s\n' "$source_mirror_action" >&2
    exit 2
fi

lock_file="$source_mirror_root/flake.lock"
if [ ! -r "$lock_file" ]; then
    printf 'error: flake.lock is missing: %s\n' "$lock_file" >&2
    exit 1
fi

if [ "${ENVY_FLAKE_SOURCE_MIRROR:-1}" = "0" ]; then
    exit 0
fi

mirror_mode="${ENVY_MIRROR:-china}"
case "$mirror_mode" in
    upstream)
        exit 0
        ;;
    china)
        ;;
    *)
        printf 'error: unsupported ENVY_MIRROR value: %s\n' "$mirror_mode" >&2
        exit 2
        ;;
esac

command -v nix >/dev/null 2>&1 || {
    printf 'error: nix is required for Flake source mirroring\n' >&2
    exit 1
}

mirror_base="${ENVY_FLAKE_MIRROR_URL:-}"
if [ -z "$mirror_base" ]; then
    mirror_base="$({
        cd "$source_mirror_root"
        nix eval --impure --raw --expr '
          let
            profile = import ./modules/mirrors/catalog.nix;
            endpoint = profile.china.flakeSourceMirror or null;
          in if endpoint == null then "" else endpoint
        '
    } 2>/dev/null || true)"
fi
if [ -z "$mirror_base" ]; then
    printf '[INFO] No Flake source mirror is configured; using locked URLs.\n' >&2
    exit 0
fi
case "$mirror_base" in
    https://*) ;;
    *)
        printf '[WARN] Refusing non-HTTPS Flake source mirror: %s\n' "$mirror_base" >&2
        exit 0
        ;;
esac
case "$mirror_base" in
    */) ;;
    *) mirror_base="$mirror_base/" ;;
esac

cache_root="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/envy/flake-sources"
mkdir -p "$cache_root"
source_mirror_signature="$({ cksum "$lock_file"; printf '%s\n' "$mirror_mode" "$mirror_base"; } | cksum | awk '{print $1}')"
marker="$cache_root/$mirror_mode-$source_mirror_signature.ready"
if [ -e "$marker" ]; then
    exit 0
fi

# Keep lock parsing inside Nix. This works before the flake itself can be
# evaluated and does not assume jq or a third-party Python installation.
source_records_expression=''
read -r -d '' source_records_expression <<'NIX_EXPR' || true
let
  lock = builtins.fromJSON (builtins.readFile ./flake.lock);
  startsWith = prefix: value:
    builtins.substring 0 (builtins.stringLength prefix) value == prefix;
  mirrorBase = builtins.getEnv "ENVY_FLAKE_MIRROR_URL";
  mirror = url: "${mirrorBase}${url}";
  lockedSource = name: node:
    let
      locked = node.locked or { };
      kind = locked.type or "";
      hash = locked.narHash or "";
      url =
        if kind == "github" then
          "https://github.com/${locked.owner}/${locked.repo}/archive/${locked.rev}.tar.gz"
        else if kind == "tarball" && startsWith "https://github.com/" locked.url then
          locked.url
        else if kind == "tarball" && startsWith "https://api.github.com/" locked.url then
          locked.url
        else
          null;
      mirrored = if url == null then null else mirror url;
    in
      if mirrored == null || hash == "" then null
      else "${name}\t${mirrored}\t${hash}";
  records = builtins.filter (record: record != null)
    (builtins.map (name: lockedSource name lock.nodes.${name}) (builtins.attrNames lock.nodes));
in
  builtins.concatStringsSep "\n" records
NIX_EXPR

source_records=""
if ! source_records="$(
    cd "$source_mirror_root"
    ENVY_FLAKE_MIRROR_URL="$mirror_base" nix eval --impure --raw \
        --expr "$source_records_expression"
)"; then
    printf '[WARN] Could not read Flake source locks; Nix will use the original URLs.\n' >&2
    exit 1
fi

if [ -z "$source_records" ]; then
    : > "$marker"
    exit 0
fi

failed=0
while IFS="$(printf '\t')" read -r input_name source_url nar_hash; do
    [ -n "$input_name" ] || continue
    case "$source_url" in
        *\?*) prefetch_url="${source_url}&narHash=${nar_hash}" ;;
        *) prefetch_url="${source_url}?narHash=${nar_hash}" ;;
    esac
    printf '[INFO] Prefetching Flake input %s through the %s mirror...\n' \
        "$input_name" "$mirror_mode" >&2
    if ! nix flake prefetch --json --no-pretty "tarball+$prefetch_url" >/dev/null; then
        printf '[WARN] Mirror fetch failed for Flake input %s; Nix will try its locked URL.\n' \
            "$input_name" >&2
        failed=1
    fi
done <<EOF_RECORDS
$source_records
EOF_RECORDS

# A partial run must be retried. Successful prefetches remain in the store and
# are reused by the next run without downloading them again.
if [ "$failed" -eq 0 ]; then
    : > "$marker"
    exit 0
fi
exit 1
