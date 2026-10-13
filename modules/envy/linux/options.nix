{ lib, ... }:

let
  inherit (lib) mkOption types;
  inherit (import ../selection-options.nix { inherit lib; }) itemSelection;
in
{
  options.envy.linux = {
    desktop = mkOption {
      type = types.enum [ "gnome" "niri" "all" "none" ];
      default = "gnome";
      description = "Linux desktop environment selection.";
    };
    option = mkOption {
      type = types.enum [ "desktop" "server" ];
      default = "desktop";
      description = "Whether the Linux machine is a desktop or server.";
    };
    software = {
      native.repositories = itemSelection "Linux native software repositories";
      native.packages = itemSelection "Linux native system packages";
      url.artifacts = itemSelection "Linux system artifacts installed from fixed URLs";
    };
  };
}
