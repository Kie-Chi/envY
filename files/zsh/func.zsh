# Keep one entrypoint for interactive shells and isolated tests.
() {
    local directory="${${(%):-%x}:A:h}/functions"
    local module
    for module in common completion packages services envy; do
        source "$directory/$module.zsh" || return
    done
}
