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
    if [ -t 1 ] && (( $+commands[bat] )); then
        command bat "$@"
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
