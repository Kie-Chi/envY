{ lib, pkgs, ... }:

let
  # The published tarball is self-contained: the CLI needs only Node's
  # built-in node:sqlite (Node >= 22.13), so no npm install step runs. Its
  # skill-install subcommand is unused; envY links the skill from the catalog.
  obelisk = pkgs.stdenvNoCC.mkDerivation (finalAttrs: {
    pname = "obelisk";
    version = "0.3.1";

    src = pkgs.fetchurl {
      url = "https://registry.npmjs.org/@obelisk-apps/cli/-/cli-${finalAttrs.version}.tgz";
      hash = "sha512-HK045c5CvO3Rs5hAsahtlEUL6NPjEzdZ9dMfjqJsxi4m11X27Yn+u1bue+YGp7ZzVBcnuxhB/izV+EqIZlWjRA==";
    };
    sourceRoot = "package";

    nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
    dontBuild = true;

    installPhase = ''
      runHook preInstall
      mkdir -p "$out/lib/obelisk" "$out/bin"
      cp -R dist package.json "$out/lib/obelisk/"
      makeBinaryWrapper ${lib.getExe pkgs.nodejs_24} "$out/bin/obelisk" \
        --add-flags "$out/lib/obelisk/dist/cli/src/obelisk.js"
      runHook postInstall
    '';

    doInstallCheck = true;
    installCheckPhase = ''
      runHook preInstallCheck
      test "$("$out/bin/obelisk" --version)" = ${lib.escapeShellArg finalAttrs.version}
      runHook postInstallCheck
    '';

    meta = {
      description = "Queryable index over local coding-agent session history";
      homepage = "https://github.com/tommy0103/obelisk";
      license = lib.licenses.agpl3Only;
      mainProgram = "obelisk";
      platforms = lib.platforms.unix;
    };
  });
in
{
  envy.software.nix.packages = {
    include = [ obelisk ];
    references.obelisk = "local:modules/agents/tools/obelisk.nix#obelisk";
  };
}
