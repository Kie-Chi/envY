{ pkgs, kind, spec, ... }:

let
  archive = pkgs.fetchurl {
    url = spec.url;
    hash = spec.integrity;
  };
  triple = if pkgs.stdenv.hostPlatform.isAarch64
    then "aarch64-unknown-linux-musl"
    else if pkgs.stdenv.hostPlatform.isx86_64
    then "x86_64-unknown-linux-musl"
    else throw "envY agent packages support only x86_64-linux and aarch64-linux";
  pname = if kind == "claude" then "claude-code" else "codex";
  mainProgram = if kind == "claude" then "claude" else "codex";
  install = if kind == "claude" then ''
    mkdir -p "$out/bin"
    tar -xzf ${archive} -C "$out" --strip-components=1 package/claude
    chmod 0755 "$out/claude"
    mv "$out/claude" "$out/bin/claude"
  '' else ''
    mkdir -p "$out/bin" "$out/lib/codex"
    tar -xzf ${archive} -C "$out/lib/codex" --strip-components=3 package/vendor/${triple}
    chmod -R u+rwX,go+rX "$out/lib/codex"
    ln -s ../lib/codex/bin/codex "$out/bin/codex"
  '';
in
assert builtins.elem kind [ "claude" "codex" ];
pkgs.runCommand "${pname}-${spec.version}" {
  nativeBuildInputs = [ pkgs.gnutar pkgs.gzip ];
  meta.mainProgram = mainProgram;
} ''
  ${install}
''
