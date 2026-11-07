{ pkgs, ... }:

# Pinned view pilots. The context package's silent-probe branch
# is replaced so opening a view never starts a model request. Extra arguments
# are accepted for callers that pass shared Pi values.
let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
  diffRuntime = fetchPiExtension {
    pname = "pi-diff-runtime";
    version = "8.0.2";
    url = "https://registry.npmjs.org/diff/-/diff-8.0.2.tgz";
    hash = "sha512-sSuxWU5j5SR9QQji/o2qMvqRNYRDOcBTgsJ/DeCf4iSN4gW+gNMXM7wFIP+fdXZxoNiAnHUTGjCr+TSWXdRDKg==";
  };
in
{
  context = fetchPiExtension {
    pname = "pi-context-view";
    version = "0.7.0";
    url = "https://registry.npmjs.org/pi-context-view/-/pi-context-view-0.7.0.tgz";
    hash = "sha512-27defDIclkYuuG6nEg47C/NZ3ld2EmAKjs05dGU7vB6Ho2UMP2ybt2dRmwmz2QPRECIld9LGojshsnV+Te0IZA==";
    postUnpack = ''
      ${pkgs.patch}/bin/patch --batch --fuzz=0 -p1 < ${../../../../files/agents/patches/pi-context-view-no-probe.patch}
    '';
  };
  diff = fetchPiExtension {
    pname = "pi-diff";
    version = "0.1.0";
    url = "https://registry.npmjs.org/@lyrumu/pi-diff/-/pi-diff-0.1.0.tgz";
    hash = "sha512-osgrVgzAjKnWHExAayH++cxhzNNnCZyBB1JQNUCinGbTnBgWaCSbJRA2S4W9lhF7j6R8ACLuavE04/F3ga5gMA==";
    # Copy the sole runtime dependency into the package; never use npm install.
    postUnpack = ''
      mkdir -p node_modules/diff
      cp -R ${diffRuntime}/. node_modules/diff/
    '';
  };
}
