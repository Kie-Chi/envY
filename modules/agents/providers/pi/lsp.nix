{ lib, pkgs, pythonEnabled ? true, ... }:

# Extra arguments are accepted so the owning Pi module may pass its package
# without making this package depend on an unused Pi build.

# Pi LSP pilot: one Python server, selected without writing or merging any
# user or project pi-lsp.json. A custom upstream config still wins; only the
# built-in fallback catalog is replaced. Runtime dependencies are unpacked
# next to the extension so Pi's Jiti loader resolves them without a global
# node_modules directory.
let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };

  dependencies = [
    {
      name = "cross-spawn";
      version = "7.0.6";
      hash = "sha512-uV2QOWP2nWzsy2aMp8aRibhi9dlzF5Hgh5SHaB9OiTGEyDTiJJyx0uy51QXdyWbtAHNua4XJzUKca3OzKUd3vA==";
    }
    {
      name = "path-key";
      version = "3.1.1";
      hash = "sha512-ojmeN0qd+y0jszEtoY48r0Peq5dwMEkIlCOu6Q5f41lfkswXuKtYrhgoTpLnyIcHm24Uhqx+5Tqm2InSwLhE6Q==";
    }
    {
      name = "shebang-command";
      version = "2.0.0";
      hash = "sha512-kHxr2zZpYtdmrN1qDjrrX/Z1rR1kG8Dx+gkpK1G4eXmvXswmcE1hTWBWYUzlraYw1/yZp6YuDY77YtvbN0dmDA==";
    }
    {
      name = "shebang-regex";
      version = "3.0.0";
      hash = "sha512-7++dFhtcx3353uBaq8DDR4NuxBetBzC7ZQOhmTQInHEd6bSrXdiEyzCvG07Z44UYdLShWUyXt5M/yhz8ekcb1A==";
    }
    {
      name = "which";
      version = "2.0.2";
      hash = "sha512-BLI3Tl1TW3Pvl70l3yq3Y64i+awpwXqsGBYWkkqMtnbXgrMD+yj7rhW0kuEDxzJaYXGjEW5ogapKNMEKNMjibA==";
    }
    {
      name = "isexe";
      version = "2.0.0";
      hash = "sha512-RHxMLp9lnKHGHRng9QFhRCMbYAcVpn69smSGcq3f36xjgVVWThj4qqLbTLlq7Ssj8B+fIQ1EuCEGI2lKsyQeIw==";
    }
  ];

  nodeModules = pkgs.runCommand "pi-lsp-node-modules-0.49.12" { } ''
    mkdir -p "$out"
    ${lib.concatMapStrings (dependency: ''
      mkdir -p "$out/${dependency.name}"
      tar -xzf ${pkgs.fetchurl {
        url = "https://registry.npmjs.org/${dependency.name}/-/${dependency.name}-${dependency.version}.tgz";
        inherit (dependency) hash;
      }} -C "$out/${dependency.name}" --strip-components=1
    '') dependencies}
  '';

  ruffServer = "${pkgs.ruff}/bin/ruff";
  patch = if pythonEnabled
    then ../../../../files/agents/patches/pi-lsp-default-ruff.patch
    else ../../../../files/agents/patches/pi-lsp-default-disabled.patch;
  serverCommand = [ ruffServer "server" ];
in
(fetchPiExtension {
  pname = "pi-lsp";
  version = "0.49.12";
  url = "https://registry.npmjs.org/@narumitw/pi-lsp/-/pi-lsp-0.49.12.tgz";
  hash = "sha512-odwv3NNK5pyq6gtslDF7FG9sfzQ8OHbu74wkG8F8bv3z4MJ3yx91HBcvMFGxFXYiJeAgF1V+uh/L35lACR/6EA==";
  postUnpack = ''
    ${pkgs.patch}/bin/patch --batch --fuzz=0 -p1 < ${patch}
    ${lib.optionalString pythonEnabled ''
      substituteInPlace dist/index.ts --replace-fail \
        '__ENVY_RUFF_SERVER__' ${lib.escapeShellArg ruffServer}
    ''}
    mkdir -p node_modules
    cp -R ${nodeModules}/. node_modules/
    test -f node_modules/cross-spawn/index.js
  '';
}).overrideAttrs (old: {
  passthru = (old.passthru or { }) // {
    inherit pythonEnabled serverCommand;
    serverPackage = pkgs.ruff;
    serverReference = "nix:ruff";
  };
})
