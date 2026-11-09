{ lib, pkgs, piPackage }:

let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
in
fetchPiExtension {
  pname = "pi-goal-x";
  # 0.34 and later require Pi 1.4; keep the locked Pi 1.1-compatible version.
  version = "0.33.0";
  url = "https://registry.npmjs.org/pi-goal-x/-/pi-goal-x-0.33.0.tgz";
  hash = "sha512-FJdd4Fz9gWNzTybovN/BJp83o5BdgOytHZIeddBxU21HNFciEpFV83yVLpJO2lFIEvOLbXLeNJtMhk138urPGA==";
  postUnpack = ''
    ${pkgs.patch}/bin/patch --batch --fuzz=0 -p1 < ${../../../../files/agents/patches/pi-goal-state-and-stages.patch}
    ${pkgs.patch}/bin/patch --batch --fuzz=0 -p1 < ${../../../../files/agents/patches/pi-goal-cache-prefix.patch}
    HOME="$TMPDIR/goal-test-home" ${lib.getExe pkgs.nodejs_24} \
      ${./tests/pi-goal.mjs} "$out" ${piPackage}/lib/pi/node_modules
  '';
}
