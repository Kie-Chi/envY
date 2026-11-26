# ==========================================
# envy Completion (typer dynamic + git bridge)
# ==========================================

_envy() {
    local -a words=("${words[@]}")
    local -i CURRENT=$CURRENT
    # Git bridge: delegate 'envy git <args>' to standard git completion
    if [[ "${words[2]}" == "git" ]]; then
        local service=git
        words=(git "${(@)words[3,-1]}")
        CURRENT=$(( CURRENT - 2 ))
        _envy_load_completion _git || return 1
        _git
        return
    fi

    # Dynamic completion via typer/Click
    (( ! $+commands[envy] )) && return 1
    eval "$(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" _ENVY_COMPLETE=complete_zsh envy)"
}

compdef _envy envy
