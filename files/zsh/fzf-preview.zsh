# fzf-tab supplies realpath separately from its displayed completion text.
zstyle ':completion:*' menu no
zstyle ':completion:*:descriptions' format '[%d]'
zstyle ':fzf-tab:*' switch-group '<' '>'
# Account for the outer border and prompt without changing other fzf widgets.
zstyle ':fzf-tab:*' fzf-min-height 12
zstyle ':fzf-tab:*' fzf-pad 4
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
