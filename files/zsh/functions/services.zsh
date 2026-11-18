# ==========================================
# Systemctl Wrapper (sc / usc)
# ==========================================
if command -v systemctl > /dev/null 2>&1; then
    _sc_build_args() {
        case "$1" in
            st)      _SC_ARGS=("status") ;;
            start|go)   _SC_ARGS=("start") ;;
            stop)    _SC_ARGS=("stop") ;;
            res)     _SC_ARGS=("restart") ;;
            rel)     _SC_ARGS=("reload") ;;
            dr)      _SC_ARGS=("daemon-reload") ;;
            en)      _SC_ARGS=("enable") ;;
            dis)     _SC_ARGS=("disable") ;;
            enn)     _SC_ARGS=("enable" "--now") ;;
            disn)    _SC_ARGS=("disable" "--now") ;;
            fail)    _SC_ARGS=("--failed") ;;
            act)     _SC_ARGS=("list-units" "--type=service" "--state=active") ;;
            *)       _SC_ARGS=("$1") ;;
        esac
    }

    sc() {
        local -a _SC_ARGS
        if [ -z "$1" ]; then sudo systemctl; return; fi
        _sc_build_args "$1"; shift
        sudo systemctl "${_SC_ARGS[@]}" "$@"
    }

    usc() {
        local -a _SC_ARGS
        if [ -z "$1" ]; then systemctl --user; return; fi
        _sc_build_args "$1"; shift
        systemctl --user "${_SC_ARGS[@]}" "$@"
    }
fi

if command -v launchctl >/dev/null 2>&1; then
    _sc_mac_logic() {
        local domain=$1
        local action=$2
        local target=$3
        shift 3

        case "$action" in
            st|status)
                if [ -z "$target" ]; then
                    launchctl list | grep -v "com.apple"
                else
                    sudo launchctl print "$domain/$target"
                fi
                ;;
            start)
                # kickstart -p
                sudo launchctl kickstart -p "$domain/$target"
                ;;
            stop)
                sudo launchctl kill SIGTERM "$domain/$target"
                ;;
            res|restart)
                sudo launchctl kickstart -k "$domain/$target"
                ;;
            en|enable)
                sudo launchctl enable "$domain/$target"
                ;;
            dis|disable)
                sudo launchctl disable "$domain/$target"
                ;;
            *)
                echo "Action $action not fully mapped for launchctl"
                ;;
        esac
    }

    sc() {
        _sc_mac_logic "system" "$1" "$2"
    }

    usc() {
        local uid=$(id -u)
        _sc_mac_logic "gui/$uid" "$1" "$2"
    }
fi

# ==========================================
# Journalctl Wrapper (jlog)
# ==========================================
if command -v journalctl >/dev/null 2>&1; then
    jlog() {
        if [ -z "$1" ]; then sudo journalctl -xe; return; fi
        if [[ "$1" == -* ]]; then sudo journalctl -xe "$@"; else local svc="$1"; shift; sudo journalctl -xe -u "$svc" "$@"; fi
    }
    ujlog() {
        if [ -z "$1" ]; then journalctl --user -xe; return; fi
        if [[ "$1" == -* ]]; then journalctl --user -xe "$@"; else local svc="$1"; shift; journalctl --user -xe -u "$svc" "$@"; fi
    }
fi

if [[ $OSTYPE == darwin* ]] && command -v log >/dev/null 2>&1; then
    jlog() {
        if [ -z "$1" ]; then
            log show --last 10m
        else
            log show --last 1h --predicate "process == '$1'" --info
        fi
    }
fi



# Execution and completion use the same shorthand expansion.
if command -v systemctl >/dev/null 2>&1; then
    _sc_comp() {
        _envy_load_completion _systemctl || return 1
        local command_name=${words[1]} service=systemctl curcontext="$curcontext"
        local -a words=("${words[@]}") _SC_ARGS
        local -i CURRENT=$CURRENT
        _sc_build_args "${words[2]}"
        words=(systemctl "${_SC_ARGS[@]}" "${words[@]:2}")
        (( CURRENT += ${#_SC_ARGS} - 1 ))
        if [[ $command_name == usc ]]; then
            words=(systemctl --user "${words[@]:1}")
            (( CURRENT++ ))
        fi
        _systemctl
    }
    compdef _sc_comp sc usc
fi

if command -v launchctl >/dev/null 2>&1; then
    _sc_mac_comp() {
        local -a actions
        actions=(
            'st:Status of service'
            'start:Start service'
            'stop:Stop service'
            'res:Restart service'
            'en:Enable service'
            'dis:Disable service'
        )

        if (( CURRENT == 2 )); then
            _describe -t actions 'launchctl actions' actions
            return
        fi

        if (( CURRENT == 3 )); then
            local -a services
            if [[ "${words[1]}" == "usc" ]]; then
                services=(
                    $(launchctl list | awk 'NR>1 && $3 !~ /com.apple/ {print $3}')
                    $(ls ~/Library/LaunchAgents 2>/dev/null | sed 's/\.plist$//')
                )
            else
                services=(
                    $(sudo launchctl list | awk 'NR>1 && $3 !~ /com.apple/ {print $3}')
                    $(ls /Library/LaunchDaemons 2>/dev/null | sed 's/\.plist$//')
                )
            fi
            services=(${(un)services})
            _describe -t services 'available services' services
        fi
    }
    compdef _sc_mac_comp sc usc
fi
