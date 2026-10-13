{ lib, machinePlatform, ... }:

assert lib.assertMsg (machinePlatform == "darwin")
  "darwin.nix requires machinePlatform = \"darwin\"";
{
  # nix-darwin composition root. Feature modules own their Darwin-specific
  # implementation; this file only assembles them for flake.nix.
  imports = [
    ./modules/envy/darwin.nix
    ./modules/software/sources.nix
    ./modules/mirrors/darwin.nix
    ./modules/agents/darwin.nix
    ./modules/desktops/darwin/nix-darwin.nix
  ];
}
