{ pkgs }:

pkgs.runCommand "envy-github-actions" {
  nativeBuildInputs = [ pkgs.zizmor ];
} ''
  export HOME="$TMPDIR/home"
  export XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir -p "$HOME" "$XDG_CACHE_HOME" .github
  ln -s ${../.github/workflows} .github/workflows
  zizmor --version
  zizmor --offline --no-config --strict-collection \
    --no-progress --color never --render-links never \
    .github/workflows
  touch "$out"
''
