{ lib, machinePlatform, ... }:

{
  imports = [ ./sources.nix
    ./npm.nix
    ./pypi.nix
  ] ++ lib.optionals (machinePlatform == "linux") [
    ./linux/repositories.nix
    ./linux/native.nix
    ./linux/artifacts.nix
  ];
}
