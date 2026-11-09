{ lib, pkgs }:

let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
in
fetchPiExtension {
  pname = "pi-powerline";
  version = "0.11.2";
  url = "https://registry.npmjs.org/pi-powerline/-/pi-powerline-0.11.2.tgz";
  hash = "sha512-33xRwqXVHSXIdZFwvCWWY9mWj9/j8MTm71aObYM5zn0LIYkAGaN52zU1m+ht3EIA558FjwyrVpNR+zgpG1n0PA==";
  postUnpack = ''
    ${lib.getExe pkgs.nodejs_24} ${../../../../files/agents/pi/powerline/adapt.mjs}
    cp ${../../../../files/agents/pi/powerline/index.ts} envy-footer.ts
    mkdir -p src
    printf 'export const MIN_PI_VERSION = "1.1.0";\n' > src/pi-version.ts
  '';
}
