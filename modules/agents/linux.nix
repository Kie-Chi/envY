{ config, lib, pkgs, ... }:

let
  isDesktop = config.envy.linux.option == "desktop";
  rtk = pkgs.callPackage ./linux/rtk.nix { };
in
{
  envy.software.nix.packages.include = [ rtk ] ++ lib.optional isDesktop pkgs.code-cursor;

  envy.software.nix.packages.references = {
    rtk = "local:modules/agents/linux/rtk.nix";
  } // lib.optionalAttrs isDesktop {
    cursor = "nix:code-cursor";
  };

}
