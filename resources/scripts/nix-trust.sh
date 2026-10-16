#!/usr/bin/env bash

set -euo pipefail

BEGIN_MARKER='# BEGIN ENVY MANAGED NIX MIRROR'
END_MARKER='# END ENVY MANAGED NIX MIRROR'
THALHEIM_URL='https://cache.thalheim.io'
THALHEIM_KEY='cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc='
USTC_URL='https://mirrors.ustc.edu.cn/nix-channels/store'
NIXOS_URL='https://cache.nixos.org/'
NIXOS_KEY='cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY='

command_name="${1:-status}"
if [ "$#" -gt 0 ]; then
    shift
fi

mirror_mode="${ENVY_MIRROR:-china}"
managed_user="${SUDO_USER:-${USER:-}}"
custom_config="${ENVY_NIX_CUSTOM_CONFIG:-/etc/nix/nix.custom.conf}"
system_config="${ENVY_NIX_SYSTEM_CONFIG:-/etc/nix/nix.conf}"
systemctl_command="${ENVY_NIX_TRUST_SYSTEMCTL:-systemctl}"
launchctl_command="${ENVY_NIX_TRUST_LAUNCHCTL:-/bin/launchctl}"
sudo_command="${ENVY_NIX_TRUST_SUDO:-sudo}"
default_daemon_socket='/nix/var/nix/daemon-socket/socket'
daemon_socket="${ENVY_NIX_TRUST_SOCKET:-$default_daemon_socket}"
nix_command="${ENVY_NIX_TRUST_NIX:-nix}"
elevated=0
quiet=0
force_reload=0
temporary_file=""
previous_file=""

usage() {
    cat <<'EOF'
Usage: nix-trust.sh status|repair [options]

Options:
  --mode MODE           Mirror profile: upstream or china
  --user NAME           User trusted to submit restricted daemon settings
  --custom-config PATH  Managed nix.custom.conf path
  --system-config PATH  Parent nix.conf (managed directly if no custom include)
  --quiet               Suppress successful/status output
  --reload              Reload the active daemon even when the block is unchanged
  --socket PATH         Daemon socket path used for readiness checks
EOF
}

fail() {
    printf '[ERROR] %s\n' "$1" >&2
    exit 2
}

say() {
    if [ "$quiet" -eq 0 ]; then
        printf '%s\n' "$1"
    fi
}

cleanup() {
    if [ -n "$temporary_file" ]; then
        rm -f "$temporary_file"
    fi
    if [ -n "$previous_file" ]; then
        rm -f "$previous_file"
    fi
}
trap cleanup EXIT HUP INT TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode)
            [ "$#" -ge 2 ] || fail "--mode requires a value"
            mirror_mode="$2"
            shift 2
            ;;
        --user)
            [ "$#" -ge 2 ] || fail "--user requires a value"
            managed_user="$2"
            shift 2
            ;;
        --custom-config)
            [ "$#" -ge 2 ] || fail "--custom-config requires a value"
            custom_config="$2"
            shift 2
            ;;
        --system-config)
            [ "$#" -ge 2 ] || fail "--system-config requires a value"
            system_config="$2"
            shift 2
            ;;
        --systemctl)
            [ "$#" -ge 2 ] || fail "--systemctl requires a value"
            systemctl_command="$2"
            shift 2
            ;;
        --launchctl)
            [ "$#" -ge 2 ] || fail "--launchctl requires a value"
            launchctl_command="$2"
            shift 2
            ;;
        --elevated)
            elevated=1
            shift
            ;;
        --quiet)
            quiet=1
            shift
            ;;
        --reload)
            force_reload=1
            shift
            ;;
        --socket)
            [ "$#" -ge 2 ] || fail "--socket requires a value"
            daemon_socket="$2"
            shift 2
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

case "$command_name" in
    status|repair)
        ;;
    *)
        usage >&2
        fail "command must be 'status' or 'repair'"
        ;;
esac

case "$mirror_mode" in
    upstream|china)
        ;;
    *)
        fail "mirror mode must be 'upstream' or 'china'"
        ;;
esac

case "$managed_user" in
    ''|*[!A-Za-z0-9._-]*)
        fail "managed user is empty or contains unsupported characters"
        ;;
esac

case "$custom_config:$system_config" in
    /*:/*)
        ;;
    *)
        fail "Nix configuration paths must be absolute"
        ;;
esac

render_block() {
    # Keep this list aligned with the explicit bootstrap NIX_CONFIG. Nix
    # daemons reject a substituter supplied by an untrusted client unless the
    # exact endpoint is present in trusted-substituters, even when it is the
    # usual cache.nixos.org default.
    local extra_substituters="$THALHEIM_URL"
    local trusted_substituters="$THALHEIM_URL"
    local keys="$THALHEIM_KEY"
    if [ "$mirror_mode" = "china" ]; then
        # Replace the installer's default substituter so the China mirror is
        # tried first, then append the remaining trusted caches in order.
        extra_substituters="$THALHEIM_URL $NIXOS_URL"
        trusted_substituters="$trusted_substituters $NIXOS_URL $USTC_URL"
        keys="$keys $NIXOS_KEY"
    fi

    printf '%s\n' \
        "$BEGIN_MARKER" \
        'extra-experimental-features = nix-command flakes' \
        'http2 = false'
    if [ "$mirror_mode" = "china" ]; then
        printf '%s\n' "substituters = $USTC_URL"
    fi
    printf '%s\n' \
        "extra-substituters = $extra_substituters" \
        "extra-trusted-substituters = $trusted_substituters" \
        "extra-trusted-public-keys = $keys" \
        "extra-trusted-users = $managed_user" \
        'download-attempts = 3' \
        "$END_MARKER"
}

select_managed_config() {
    if [ ! -r "$system_config" ]; then
        fail "$system_config is not readable; cannot locate the active Nix configuration"
    fi
    managed_config="$custom_config"
    if ! awk -v custom="$custom_config" -v parent="$(dirname "$system_config")" '
        ($1 == "include" || $1 == "!include") && (NF == 2 || $3 ~ /^#/) {
            path = $2
            if (path !~ /^\//) path = parent "/" path
            if (path == custom) found = 1
        }
        END { exit !found }
    ' "$system_config"; then
        # The official installer has no nix.custom.conf include. Keep its
        # configuration intact and own only a marked block in the active file.
        managed_config="$system_config"
    fi
    if [ -L "$managed_config" ]; then
        fail "$managed_config is a symlink; refusing to replace externally managed Nix configuration"
    fi
}

marker_count() {
    local marker="$1"
    local count
    if [ ! -e "$managed_config" ]; then
        printf '0\n'
        return
    fi
    count="$(grep -Fxc "$marker" "$managed_config" || true)"
    printf '%s\n' "$count"
}

validate_markers() {
    local begin_count end_count begin_line end_line
    begin_count="$(marker_count "$BEGIN_MARKER")"
    end_count="$(marker_count "$END_MARKER")"
    if [ "$begin_count" -ne "$end_count" ] || [ "$begin_count" -gt 1 ]; then
        fail "$managed_config contains malformed or duplicate envY mirror markers"
    fi
    if [ "$begin_count" -eq 1 ]; then
        begin_line="$(grep -Fnx "$BEGIN_MARKER" "$managed_config" | cut -d: -f1)"
        end_line="$(grep -Fnx "$END_MARKER" "$managed_config" | cut -d: -f1)"
        if [ "$begin_line" -ge "$end_line" ]; then
            fail "$managed_config contains envY mirror markers in the wrong order"
        fi
    fi
}

render_candidate() {
    local output="$1"
    local begin_count
    begin_count="$(marker_count "$BEGIN_MARKER")"
    : > "$output"

    if [ "$begin_count" -eq 1 ]; then
        {
            awk -v marker="$BEGIN_MARKER" '$0 == marker { exit } { print }' \
                "$managed_config"
            render_block
            awk -v marker="$END_MARKER" \
                'found { print } $0 == marker { found = 1; next }' \
                "$managed_config"
        } >> "$output"
        return
    fi

    {
        if [ -e "$managed_config" ]; then
            cat "$managed_config"
            if [ -s "$managed_config" ] && [ "$(tail -c 1 "$managed_config" | wc -l | tr -d ' ')" -eq 0 ]; then
                printf '\n'
            fi
        fi
        render_block
    } >> "$output"
}

make_candidate() {
    temporary_file="$(mktemp "${TMPDIR:-/tmp}/envy-nix-trust.XXXXXX")"
    render_candidate "$temporary_file"
}

is_ready() {
    validate_markers
    make_candidate
    if [ -f "$managed_config" ] && cmp -s "$temporary_file" "$managed_config"; then
        rm -f "$temporary_file"
        temporary_file=""
        return 0
    fi
    rm -f "$temporary_file"
    temporary_file=""
    return 1
}

daemon_socket_ready() {
    local socket_path
    if [ "$(uname -s)" = "Darwin" ] && [ "$daemon_socket" = "$default_daemon_socket" ]; then
        for socket_path in /var/run/nix-daemon.socket "$default_daemon_socket"; do
            if [ -S "$socket_path" ]; then
                return 0
            fi
        done
        return 1
    fi
    [ -S "$daemon_socket" ]
}

daemon_ready() {
    daemon_socket_ready || return 1
    command -v "$nix_command" >/dev/null 2>&1 || return 1
    "$nix_command" store info --store daemon >/dev/null 2>&1
}

restart_active_daemon() {
    local service
    local attempt
    if [ "$(uname -s)" = "Darwin" ]; then
        if ! command -v "$launchctl_command" >/dev/null 2>&1; then
            return 1
        fi
        attempt=1
        while [ "$attempt" -le 10 ]; do
            for service in org.nixos.nix-daemon systems.determinate.nix-daemon; do
                if "$launchctl_command" print "system/$service" >/dev/null 2>&1; then
                    "$launchctl_command" kickstart -k "system/$service" || return "$?"
                    # launchd can report the job before the restarted daemon
                    # has registered its socket. Wait for the socket itself so
                    # the first Nix call cannot race the daemon reload.
                    attempt=1
                    while [ "$attempt" -le 15 ]; do
                        if "$launchctl_command" print "system/$service" >/dev/null 2>&1 \
                            && daemon_ready; then
                            say "[OK] Restarted $service after updating Nix daemon trust."
                            return 0
                        fi
                        sleep 1
                        attempt=$((attempt + 1))
                    done
                    return 1
                fi
            done
            sleep 1
            attempt=$((attempt + 1))
        done
        return 1
    fi
    if ! command -v "$systemctl_command" >/dev/null 2>&1; then
        return 1
    fi
    for service in nix-daemon.service determinate-nixd.service; do
        if "$systemctl_command" is-active --quiet "$service" 2>/dev/null; then
            "$systemctl_command" restart "$service" || return "$?"
            attempt=1
            while [ "$attempt" -le 15 ]; do
                if "$systemctl_command" is-active --quiet "$service" 2>/dev/null \
                    && daemon_ready; then
                    say "[OK] Restarted $service after updating Nix daemon trust."
                    return 0
                fi
                sleep 1
                attempt=$((attempt + 1))
            done
            return 1
        fi
    done
    return 1
}

repair_as_current_user() {
    local config_dir
    config_dir="$(dirname "$managed_config")"
    mkdir -p "$config_dir"
    temporary_file="$(mktemp "$config_dir/.envy-nix-custom.XXXXXX")"
    if [ -f "$managed_config" ]; then
        cp -p "$managed_config" "$temporary_file"
    else
        chmod 0644 "$temporary_file"
    fi
    render_candidate "$temporary_file"

    if [ -f "$managed_config" ] && cmp -s "$temporary_file" "$managed_config"; then
        rm -f "$temporary_file"
        temporary_file=""
        if [ "$force_reload" -eq 1 ]; then
            if ! restart_active_daemon; then
                fail "Nix daemon reload failed; configuration was left unchanged"
            fi
        fi
        say "[OK] Nix daemon trust is already ready: $managed_config"
        return
    fi

    if [ -f "$managed_config" ]; then
        previous_file="$(mktemp "$config_dir/.envy-nix-previous.XXXXXX")"
        cp -p "$managed_config" "$previous_file"
    fi
    mv -f "$temporary_file" "$managed_config"
    temporary_file=""
    if ! restart_active_daemon; then
        if [ -n "$previous_file" ]; then
            mv -f "$previous_file" "$managed_config"
            previous_file=""
        else
            rm -f "$managed_config"
        fi
        fail "Nix daemon restart failed or no active daemon was found; restored the previous configuration"
    fi
    say "[OK] Updated Nix daemon trust: $managed_config"
}

select_managed_config
if is_ready && [ "$force_reload" -eq 0 ]; then
    if daemon_ready; then
        say "[OK] Nix daemon trust is ready: $managed_config"
        exit 0
    fi
    if [ "$command_name" = "status" ]; then
        say "[INFO] Nix daemon trust is configured but the daemon is not ready: $managed_config"
        exit 1
    fi
    # Reuse the normal elevation path below. A user can inspect a ready
    # configuration, but restarting a system daemon still needs root on
    # Darwin and on most Linux installations.
    force_reload=1
fi

if [ "$command_name" = "status" ]; then
    say "[INFO] Nix daemon trust requires repair: $managed_config"
    exit 1
fi

config_dir="$(dirname "$managed_config")"
if [ "$(id -u)" -eq 0 ] || { [ -d "$config_dir" ] && [ -w "$config_dir" ]; }; then
    repair_as_current_user
    exit 0
fi

if [ "$elevated" -eq 1 ]; then
    fail "elevated process still cannot write $config_dir"
fi
if ! command -v "$sudo_command" >/dev/null 2>&1; then
    fail "Nix daemon trust requires root access, but sudo is unavailable"
fi

bash_command="$(command -v bash)"
if [ "$quiet" -eq 1 ] && [ "$force_reload" -eq 1 ]; then
    exec "$sudo_command" -- "$bash_command" "$0" repair \
        --mode "$mirror_mode" \
        --user "$managed_user" \
        --custom-config "$custom_config" \
        --system-config "$system_config" \
        --systemctl "$systemctl_command" \
        --launchctl "$launchctl_command" \
        --socket "$daemon_socket" \
        --quiet --reload --elevated
elif [ "$quiet" -eq 1 ]; then
    exec "$sudo_command" -- "$bash_command" "$0" repair \
        --mode "$mirror_mode" \
        --user "$managed_user" \
        --custom-config "$custom_config" \
        --system-config "$system_config" \
        --systemctl "$systemctl_command" \
        --launchctl "$launchctl_command" \
        --socket "$daemon_socket" \
        --quiet --elevated
elif [ "$force_reload" -eq 1 ]; then
    exec "$sudo_command" -- "$bash_command" "$0" repair \
        --mode "$mirror_mode" \
        --user "$managed_user" \
        --custom-config "$custom_config" \
        --system-config "$system_config" \
        --systemctl "$systemctl_command" \
        --launchctl "$launchctl_command" \
        --socket "$daemon_socket" \
        --reload --elevated
fi
exec "$sudo_command" -- "$bash_command" "$0" repair \
    --mode "$mirror_mode" \
    --user "$managed_user" \
    --custom-config "$custom_config" \
    --system-config "$system_config" \
    --systemctl "$systemctl_command" \
    --launchctl "$launchctl_command" \
    --socket "$daemon_socket" \
    --elevated
