{ pkgs, ... }:

{
  imports = [
    ./pkg.nix
    ./net-pkg.nix
  ];
  envy.software.nix.packages.include = with pkgs; [
    (pkgs.callPackage ./tod.nix {})
  ];
  envy.software.nix.packages.references.tod = "local:modules/devps/linux/pkgs/tod.nix";
}
