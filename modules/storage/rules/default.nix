{ home, machinePlatform }:

let
  common = import ./common.nix { inherit home; };
  platform = import (if machinePlatform == "darwin" then ./darwin.nix else ./linux.nix) {
    inherit home;
  };
in
assert builtins.intersectAttrs common platform == { };
{
  rules = common // platform;
  sources = builtins.mapAttrs (_: _: "default:common") common
    // builtins.mapAttrs (_: _: "default:${machinePlatform}") platform;
}
