{ pkgs, lib, ... }:

let
  # Helper Function to package a macOS App from a .dmg archive
  #   - pname:       The package name (e.g., "v2rayN").
  #   - version:     The package version (e.g., "7.17.3").
  #   - url:         The full URL to the .dmg file. You can use `${version}` inside.
  #   - hash:        The SHA256 hash of the .dmg file.
  #   - appName:     (Optional) The name of the .app bundle inside the dmg. If not provided, uses "*.app".
  #   - description: (Optional) Description for the package metadata.
  #   - homepage:    (Optional) Homepage URL for the package metadata.
  makeMacDmgApp = { pname, version, url, hash, appName ? "*.app", description ? "", homepage ? "" }:
    pkgs.stdenv.mkDerivation rec {
      inherit pname version;
      src = pkgs.fetchurl {
        inherit url hash;
      };
      nativeBuildInputs = [ pkgs.undmg ];
      sourceRoot = ".";
      installPhase = ''
        mkdir -p $out/Applications
        cp -r ${appName} $out/Applications/
      '';
      dontStrip = true;
      dontFixup = true;
      dontPatchELF = true;
      meta = with lib; {
        inherit description homepage;
        platforms = platforms.darwin;
      };
    };

  okular = makeMacDmgApp rec {
    pname = "okular";
    version = "26.04";
    url = "https://cdn.kde.org/ci-builds/graphics/okular/release-${version}/macos-arm64/okular-release_${version}-7455-macos-clang-arm64.dmg";
    hash = "sha256-hzRm1mtgABdIos6hZ03F3bzYvUa+IUsNhFtb8LwheJc=";
    description = "A PDF viewer for KDE";
    homepage = "https://okular.kde.org/";
  };
in
{
  envy.software.nix.packages.include = [ okular ];
  envy.software.nix.packages.references.okular = "local:modules/desktops/darwin/dmgs.nix#okular";
}
