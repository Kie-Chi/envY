{ lib, ... }@moduleArgs:
let
  softwareSourceBindings = moduleArgs.softwareSourceBindings or { };
  softwareSourcePlatforms = moduleArgs.softwareSourcePlatforms or { };
  entry = label: ecosystem: type: parameters: {
    inherit label ecosystem type parameters;
  };
in {
  envy.software.sources = {
    "native:system" = (entry "System repositories" "native" "native" { system = true; }) // { scope = "system"; platforms = [ "linux" ]; };
    "nix:envy-local" = (entry "envY local derivations" "nix" "local" { path = "."; }) // { scope = "project"; };
    "homebrew:homebrew/core" = (entry "Homebrew core" "homebrew" "tap" { tap = "homebrew/core"; }) // { platforms = [ "darwin" ]; scope = "system"; };
    "homebrew:homebrew/cask" = (entry "Homebrew cask" "homebrew" "tap" { tap = "homebrew/cask"; }) // { platforms = [ "darwin" ]; scope = "system"; };
    "npm:npmjs" = entry "npm" "npm" "registry" { registry = "https://registry.npmjs.org"; };
    "pypi:pypi" = entry "PyPI" "pypi" "registry" { index = "https://pypi.org/simple"; metadata = "https://pypi.org/pypi"; };
    "cargo:crates.io" = entry "crates.io" "cargo" "registry" { metadata = "https://crates.io"; discoveryOnly = true; };
    "go:pkg.go.dev" = entry "Go package discovery" "go" "registry" { metadata = "https://pkg.go.dev"; discoveryOnly = true; };
  } // lib.mapAttrs' (name: binding: lib.nameValuePair ("nix:" + name)
    ((entry name "nix" "flake" binding) // {
      scope = "project"; owner = "shared-flake";
      platforms = softwareSourcePlatforms.${name} or [ "darwin" "linux" ];
    })) softwareSourceBindings;
}
