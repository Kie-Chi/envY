{ runCommand, systemd, zsh }:

# Ship only the completion script, without adding systemd executables to PATH.
runCommand "envy-systemd-completions-${systemd.version}" {
  nativeBuildInputs = [ zsh ];
} ''
  install -Dm644 ${systemd.src}/shell-completion/zsh/_systemctl.in \
    "$out/share/zsh/site-functions/_systemctl"
  # Resolve host tools through PATH, as upstream does for systemctl/systemd-path.
  substituteInPlace "$out/share/zsh/site-functions/_systemctl" \
    --replace-fail '{{LIBEXECDIR}}/systemd' 'systemd'
  cp ${systemd.src}/shell-completion/zsh/_sd_* "$out/share/zsh/site-functions/"
  for completion in "$out"/share/zsh/site-functions/*; do
    zsh -n "$completion"
  done
''
