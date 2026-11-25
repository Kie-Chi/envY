#!/usr/bin/env bash
set -euo pipefail

app_directory="$1"

# linkApps directories are removed by linkGeneration before copyApps runs.
if [[ -L "$app_directory" || ! -d "$app_directory" ]]; then
  exit 0
fi

for app_bundle in "$app_directory/"*.app; do
  [[ -d "$app_bundle" ]] || continue

  # copyApps replaces legacy store links with writable application copies.
  # Writing through these links tests store immutability, not TCC permission.
  if [[ -L "$app_bundle" ]]; then
    resolved_bundle="$(cd -- "$app_bundle" && pwd -P)"
    if [[ "$resolved_bundle" == /nix/store/* ]]; then
      continue
    fi
  fi

  if diagnostic=$(/usr/bin/touch "$app_bundle/.DS_Store" 2>&1); then
    continue
  fi

  printf >&2 'error: cannot update application: %s\n' "$app_bundle"
  printf >&2 '%s\n' "$diagnostic"
  printf >&2 'Grant App Management permission to the terminal or host application in System Settings > Privacy & Security > App Management.\n'
  printf >&2 'For SSH activation, check Full Disk Access for remote users in System Settings > General > Sharing > Remote Login.\n'
  printf >&2 'Existing privacy permissions have been preserved.\n'
  exit 1
done
