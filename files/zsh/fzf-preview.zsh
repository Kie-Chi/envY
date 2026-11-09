# fzf-tab supplies realpath separately from its displayed completion text.
zstyle ':completion:*' menu no
zstyle ':fzf-tab:*' switch-group '<' '>'
zstyle ':fzf-tab:complete:*:*' fzf-preview '
  if [[ -d "$realpath" ]]; then
    if (( $+commands[eza] )); then
      command eza --all --oneline --color=always --icons=always --group-directories-first -- "$realpath"
    else
      command ls -A -- "$realpath"
    fi
  elif [[ -f "$realpath" ]]; then
    if (( $+commands[bat] )); then
      command bat --color=always --style=numbers --paging=never --line-range=:160 -- "$realpath"
    else
      command head -n 160 -- "$realpath"
    fi
  fi
'
