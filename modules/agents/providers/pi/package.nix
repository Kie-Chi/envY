{ lib, moreThinking, piPackage }:

# Patch Pi's workspace build, which compiles both the library and the bundled
# CLI. Upstream reads package and lock files from `source` while evaluating,
# so a patched source derivation would force import-from-derivation and make
# machines of the other platform unevaluable. Intercepting the workspace
# mkDerivation keeps `source` a plain path; the install checks below fail the
# build if the patches stop landing after an upstream change.
let
  patches = [ ../../../../files/agents/patches/pi-anthropic-sse-crlf.patch ]
    ++ lib.optional moreThinking ../../../../files/agents/patches/pi-ultra-thinking.patch;
in
(piPackage.override (args: {
  stdenv = args.stdenv // {
    mkDerivation = attrs: args.stdenv.mkDerivation (
      if builtins.isAttrs attrs && (attrs.pname or null) == "pi-workspace-packages"
      then attrs // { patches = (attrs.patches or [ ]) ++ patches; }
      else attrs);
  };
})).overrideAttrs (old: {
  postInstallCheck = (old.postInstallCheck or "") + ''
    node ${./tests/pi-sse-crlf.mjs} "$out/lib/pi/node_modules"
  '' + lib.optionalString moreThinking ''
    node ${./tests/pi-thinking.mjs} "$out/lib/pi/node_modules"
  '';
})
