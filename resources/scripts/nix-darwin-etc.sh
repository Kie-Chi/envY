#!/usr/bin/env bash

# Move unmanaged shell startup files out of nix-darwin's way before activation.
# The nix-darwin checks phase runs before activation scripts, so this must be
# invoked by the apply wrapper itself.
set -euo pipefail

etc_root="${ENVY_NIX_DARWIN_ETC_ROOT:-/etc}"

migration_names="bashrc zshrc"
for name in $migration_names; do
    file="$etc_root/$name"
    backup="$file.before-nix-darwin"
    static="$etc_root/static/$name"

    if [ -L "$file" ]; then
        link_target="$(readlink "$file")"
        if [ "$link_target" = "$static" ]; then
            continue
        fi
    elif [ ! -e "$file" ]; then
        continue
    fi

    if [ -e "$backup" ] || [ -L "$backup" ]; then
        printf 'error: refusing to overwrite existing backup %s\n' "$backup" >&2
        exit 1
    fi
done

moved_names=""
for name in $migration_names; do
    file="$etc_root/$name"
    backup="$file.before-nix-darwin"
    static="$etc_root/static/$name"

    if [ -L "$file" ] && [ "$(readlink "$file")" = "$static" ]; then
        continue
    fi
    if [ ! -e "$file" ] && [ ! -L "$file" ]; then
        continue
    fi

    printf 'moving %s to %s\n' "$file" "$backup" >&2
    if mv "$file" "$backup"; then
        moved_names="$name $moved_names"
    else
        for moved_name in $moved_names; do
            moved_file="$etc_root/$moved_name"
            moved_backup="$moved_file.before-nix-darwin"
            mv "$moved_backup" "$moved_file" 2>/dev/null || true
        done
        exit 1
    fi
done
