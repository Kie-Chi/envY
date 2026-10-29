{ piPackage, pkgs }:

# Patch the upstream workspace source, so both the library and bundled CLI are
# rebuilt together. The outer Pi derivation's src is only its install lock.
(piPackage.override (args: {
  source = pkgs.applyPatches {
    name = "pi-source-ultra-thinking";
    src = args.source;
    patches = [ ../../../../files/agents/patches/pi-ultra-thinking.patch ];
  };
})).overrideAttrs (old: {
  postInstallCheck = (old.postInstallCheck or "") + ''
    node ${./tests/pi-thinking.mjs} "$out/lib/pi/node_modules"
  '';
})
