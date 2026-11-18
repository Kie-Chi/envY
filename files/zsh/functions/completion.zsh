# Autoload registration alone does not guarantee that a function can be loaded.
_envy_load_completion() {
    local name=$1
    if (( $+functions[$name] )) && [[ ${functions[$name]} != *'builtin autoload -X'* ]]; then
        return 0
    fi
    autoload -Uz +X "$name" 2>/dev/null
}
