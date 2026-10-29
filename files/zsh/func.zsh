# Function for zsh

# ==========================================
# Base Function
# ==========================================

copypath() {
  # If no argument passed, use current directory
  local file="${1:-.}"

  # If argument is not an absolute path, prepend current directory
  [[ $file = /* ]] || file="$PWD/$file"

  # Copy the absolute path to clipboard
  # Detect OS and use appropriate clipboard command
  if [[ "$OSTYPE" == darwin* ]]; then
    echo -n "$file" | pbcopy
  elif [[ "$OSTYPE" == cygwin* ]]; then
    echo -n "$file" > /dev/clipboard
  else
    if command -v xclip > /dev/null; then
      echo -n "$file" | xclip -selection clipboard
    elif command -v xsel > /dev/null; then
      echo -n "$file" | xsel --clipboard --input
    else
      print "clipcopy: Platform $OSTYPE not supported or xclip/xsel not installed" >&2
      return 1
    fi
  fi
  
  echo "Copied path to clipboard: $file"
}

# Alias it if you want
alias cpd='copypath'

copyfile() {
  # Copy file content to clipboard
  local file="$1"

  if [ -z "$file" ]; then
    print "copyfile: no file specified" >&2
    return 1
  fi

  if [ ! -f "$file" ]; then
    print "copyfile: $file not found" >&2
    return 1
  fi

  # Detect OS and use appropriate clipboard command
  if [[ "$OSTYPE" == darwin* ]]; then
    cat "$file" | pbcopy
  else
    if command -v xclip > /dev/null; then
      cat "$file" | xclip -selection clipboard
    elif command -v xsel > /dev/null; then
      cat "$file" | xsel --clipboard --input
    elif command -v copyq > /dev/null; then
      copyq copy "$(cat "$file")"
    else
      print "copyfile: no clipboard tool found (xclip/xsel/copyq)" >&2
      return 1
    fi
  fi

  echo "Copied file content to clipboard: $file"
}

alias cpf='copyfile'


mkcd() {
    mkdir -p "$1" && cd "$1"
}

mypy() {
    if [[ "$CONDA_DEFAULT_ENV" != "$LIKED_CONDA_ENV" ]]; then
        echo "Activating $LIKED_CONDA_ENV..."
        conda activate $LIKED_CONDA_ENV
    fi
    python3 "$@"
}

extract() {
    if [ -f $1 ] ; then
        case $1 in
            *.tar.bz2)   tar xjf $1     ;;
            *.tar.gz)    tar xzf $1     ;;
            *.bz2)       bunzip2 $1     ;;
            *.rar)       unrar e $1     ;;
            *.gz)        gunzip $1      ;;
            *.tar)       tar xf $1      ;;
            *.tbz2)      tar xjf $1     ;;
            *.tgz)       tar xzf $1     ;;
            *.zip)       unzip $1       ;;
            *.Z)         uncompress $1  ;;
            *.7z)        7z x $1        ;;
            *)           echo "'$1' cannot be extracted via extract()" ;;
        esac
    else
        echo "'$1' is not a valid file"
    fi
}
alias ec='extract'

function cat() {
    if [ -t 1 ]; then
        bat "$@"
    else
        command cat "$@"
    fi
}
alias c='cat'

# Resolve user-level commands to absolute paths before invoking real sudo,
# so that binaries outside the secure_path (e.g. ~/.local/bin/rtk,
# ~/.nix-profile/bin/envy) remain accessible under sudo.
sudo() {
    local -a new_args=()
    local state=flags  # flags | skip | cmd_next | cmd

    for arg in "$@"; do
        case "$state" in
            skip)   # argument of a flag that takes a value (e.g. -u root)
                new_args+=("$arg")
                state=flags
                ;;
            cmd_next)  # first arg after -- separator = the command
                local resolved
                resolved="$(whence -p "$arg")"
                new_args+=("${resolved:-$arg}")
                state=cmd
                ;;
            cmd)    # remaining command arguments — pass through
                new_args+=("$arg")
                ;;
            flags)
                case "$arg" in
                    -[uUgCrt])  # flags that consume the next argument
                        new_args+=("$arg")
                        state=skip
                        ;;
                    --)         # end-of-options marker
                        new_args+=("$arg")
                        state=cmd_next
                        ;;
                    -*)         # standalone flags (-E, -i, -s, -n, etc.)
                        new_args+=("$arg")
                        ;;
                    *)          # first non-flag argument = the command
                        local resolved
                        resolved="$(whence -p "$arg")"
                        new_args+=("${resolved:-$arg}")
                        state=cmd
                        ;;
                esac
                ;;
        esac
    done

    command sudo "${new_args[@]}"
}


# ==========================================
# PM Wrapper
# ==========================================
pm() {
    if [[ -z "$1" ]]; then
        echo "Usage: pm [i|s|rm|up|ug|cl] <args>"
        echo "  i   : install"
        echo "  s   : search"
        echo "  rm  : remove"
        echo "  up  : update"
        echo "  ug  : upgrade"
        return 1
    fi
    local action=$1
    shift
    local _install _search _remove _update _upgrade _clean

    if command -v brew &> /dev/null; then
        # macOS Homebrew
        _install="brew install"
        _search="brew search"
        _remove="brew uninstall"
        _update="brew update"
        _upgrade="brew upgrade"
        _clean="brew cleanup"
    elif command -v apt &> /dev/null; then
        # Debian / Ubuntu
        _install="sudo apt install"
        _search="apt search"
        _remove="sudo apt remove"
        _update="sudo apt update"
        _upgrade="sudo apt upgrade"
        _clean="sudo apt autoremove && sudo apt clean"
    elif command -v dnf &> /dev/null; then
        # Fedora / RHEL 8+
        _install="sudo dnf install"
        _search="dnf search"
        _remove="sudo dnf remove"
        _update="sudo dnf check-update"
        _upgrade="sudo dnf upgrade"
        _clean="sudo dnf autoremove && sudo dnf clean all"
    elif command -v pacman &> /dev/null; then
        # Arch Linux / Manjaro
        _install="sudo pacman -S"
        _search="pacman -Ss"
        _remove="sudo pacman -Rs"
        _update="sudo pacman -Sy"
        _upgrade="sudo pacman -Syu"
        _clean="sudo pacman -Sc"
    else
        echo "Package Manager Not in (apt, dnf, pacman)"
        return 1
    fi

    case "$action" in
        i|in|install)
            ${=_install} "$@" ;;
        s|se|search)
            ${=_search} "$@" ;;
        r|rm|remove|un|uninstall)
            ${=_remove} "$@" ;;
        up|update)
            ${=_update} ;;
        ug|upgrade)
            ${=_upgrade} ;;
        cl|clean)
            eval "$_clean" ;;
        *)
            echo "Usage: pm [i|s|rm|up|ug|cl] <args>"
            echo "  i   : install"
            echo "  s   : search"
            echo "  rm  : remove"
            echo "  up  : update"
            echo "  ug  : upgrade"
            return 1
            ;;
    esac
}

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
        if [ -z "$1" ]; then sudo systemctl; return; fi
        _sc_build_args "$1"; shift
        sudo systemctl "${_SC_ARGS[@]}" "$@"
    }

    usc() {
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

if command -v log >/dev/null 2>&1; then
    jlog() {
        if [ -z "$1" ]; then
            log show --last 10m
        else
            log show --last 1h --predicate "process == '$1'" --info
        fi
    }
fi


###########################################
###########################################

# ==========================================
# Zsh Completion & Middleware
# ==========================================
# oh-my-zsh has already run compinit; the compdef calls below rely on it.

# 1. SC Completion
if command -v systemctl > /dev/null 2>&1; then
    _sc_comp() {
        if ! (( $+functions[_systemctl] )); then
            autoload -U _systemctl
            if ! (( $+functions[_systemctl] )); then
                return 1
            fi
        fi

        case "${words[2]}" in
            st)   words[2]="status" ;;
            res)  words[2]="restart" ;;
            rel)  words[2]="reload" ;;
            dr)   words[2]="daemon-reload" ;;
            en)   words[2]="enable" ;;
            dis)  words[2]="disable" ;;
            enn)  words[2]="enable" ;;
            disn) words[2]="disable" ;;
            fail) words[2]="--failed" ;;
            act)  words[2]="list-units" ;;
        esac
        if [[ "${words[1]}" == "usc" ]]; then
            words=("systemctl" "--user" "${words[@]:1}")
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

# 2. PM Completion
_pm() {
    local -a commands
    commands=(
        'i:Install package'
        's:Search package'
        'rm:Remove package'
        'up:Update repositories'
        'ug:Upgrade system'
        'cl:Clean cache'
    )

    if (( CURRENT == 2 )); then
        _describe -t commands 'pm commands' commands
        return
    fi

    local tool
    if command -v apt &> /dev/null; then tool="apt"
    elif command -v dnf &> /dev/null; then tool="dnf"
    elif command -v pacman &> /dev/null; then tool="pacman"
    elif command -v brew &> /dev/null; then tool="brew"
    else return 1; fi
    local curcontext="$curcontext" 
    local service="$tool"
    (( $+functions[_$tool] )) || autoload -U _$tool
    words[1]=$tool
    
    local sub_cmd="${words[2]}"
    case "$sub_cmd" in
        i)
            [[ "$tool" == "pacman" ]] && words[2]="-S" || words[2]="install"
            ;;
        s)
            [[ "$tool" == "pacman" ]] && words[2]="-Ss" || words[2]="search"
            ;;
        rm)
            [[ "$tool" == "pacman" ]] && words[2]="-Rs" || words[2]="remove"
            ;;
        up)
            if [[ "$tool" == "pacman" ]]; then words[2]="-Sy"; elif [[ "$tool" == "dnf" ]]; then words[2]="check-update"; else words[2]="update"; fi
            ;;
        ug)
            [[ "$tool" == "pacman" ]] && words[2]="-Syu" || words[2]="upgrade"
            ;;
        *)
            return 1
            ;;
    esac
    _$tool
}
compdef _pm pm

# ==========================================
# envy Completion (typer dynamic + git bridge)
# ==========================================

_envy() {
    # Git bridge: delegate 'envy git <args>' to standard git completion
    if [[ "${words[2]}" == "git" ]]; then
        local service=git
        words=(git "${(@)words[3,-1]}")
        CURRENT=$(( CURRENT - 2 ))
        _git
        return
    fi

    # Dynamic completion via typer/Click
    (( ! $+commands[envy] )) && return 1
    eval "$(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" _ENVY_COMPLETE=complete_zsh envy)"
}

compdef _envy envy
