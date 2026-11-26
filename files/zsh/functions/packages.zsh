# Backend selection and action expansion are shared by execution and completion.
_pm_select_tool() {
    local tool
    for tool in brew apt dnf pacman; do
        if command -v "$tool" >/dev/null 2>&1; then
            REPLY=$tool
            return 0
        fi
    done
    return 1
}

_pm_build_args() {
    local tool=$1 action=$2
    _PM_ARGS=()
    _PM_EXTRA_ARGS=()
    case $action in
        i|in|install) _PM_ACTION=install ;;
        s|se|search) _PM_ACTION=search ;;
        r|rm|remove|un|uninstall) _PM_ACTION=remove ;;
        up|update) _PM_ACTION=update ;;
        ug|upgrade) _PM_ACTION=upgrade ;;
        cl|clean) _PM_ACTION=clean ;;
        *) return 1 ;;
    esac
    case "$tool:$_PM_ACTION" in
        brew:remove) _PM_ARGS=(uninstall) ;;
        brew:clean) _PM_ARGS=(cleanup) ;;
        dnf:update) _PM_ARGS=(check-update) ;;
        apt:clean) _PM_ARGS=(autoremove); _PM_EXTRA_ARGS=(clean) ;;
        dnf:clean) _PM_ARGS=(autoremove); _PM_EXTRA_ARGS=(clean all) ;;
        pacman:install) _PM_ARGS=(-S) ;;
        pacman:search) _PM_ARGS=(-Ss) ;;
        pacman:remove) _PM_ARGS=(-Rs) ;;
        pacman:update) _PM_ARGS=(-Sy) ;;
        pacman:upgrade) _PM_ARGS=(-Syu) ;;
        pacman:clean) _PM_ARGS=(-Sc) ;;
        *) _PM_ARGS=("$_PM_ACTION") ;;
    esac
}

_pm_usage() {
    print -r -- 'Usage: pm [i|s|rm|up|ug|cl] <args>'
    print -r -- '  i   : install' '  s   : search' '  rm  : remove' \
        '  up  : update' '  ug  : upgrade' '  cl  : clean cache'
}

pm() {
    if (( $# == 0 )); then _pm_usage; return 1; fi
    local action=$1 REPLY tool _PM_ACTION
    local -a _PM_ARGS _PM_EXTRA_ARGS
    shift
    if ! _pm_select_tool; then
        print -r -- 'Package Manager Not in (brew, apt, dnf, pacman)'
        return 1
    fi
    tool=$REPLY
    if ! _pm_build_args "$tool" "$action"; then _pm_usage; return 1; fi
    local -a command=("$tool")
    if [[ $tool != brew && $_PM_ACTION != search ]]; then
        command=(sudo "$tool")
    fi
    case $_PM_ACTION in
        install|search|remove) "${command[@]}" "${_PM_ARGS[@]}" "$@" ;;
        *)
            "${command[@]}" "${_PM_ARGS[@]}" || return $?
            if (( ${#_PM_EXTRA_ARGS} )); then
                "${command[@]}" "${_PM_EXTRA_ARGS[@]}"
            fi
            ;;
    esac
}

_pm() {
    local -a words=("${words[@]}") _PM_ARGS _PM_EXTRA_ARGS
    local -i CURRENT=$CURRENT
    if (( CURRENT == 2 )); then
        local -a actions=(
            'i:Install package' 's:Search package' 'rm:Remove package'
            'up:Update repositories' 'ug:Upgrade system' 'cl:Clean cache'
        )
        _describe -t commands 'pm commands' actions
        return
    fi
    local REPLY tool _PM_ACTION
    _pm_select_tool || return 1
    tool=$REPLY
    _pm_build_args "$tool" "${words[2]}" || return 1
    local curcontext="$curcontext" service=$tool
    _envy_load_completion "_$tool" || return 1
    words=("$tool" "${_PM_ARGS[@]}" "${words[@]:2}")
    (( CURRENT += ${#_PM_ARGS} - 1 ))
    "_$tool"
}
compdef _pm pm
