{ pkgs, config, lib, ... }:

let
  profile-uuid = "2b7c4080-0ddd-46c5-8f23-563fd3ba789d";
  desktop = config.envy.linux.desktop;
  enabled = config.envy.linux.option == "desktop"
    && (desktop == "gnome" || desktop == "all");
  alacrittySelected = builtins.elem "alacritty" (map lib.getName config.envy.software.nix.packages.effective);
  palette = import ../../cores/tokyo-night.nix;
in
{
  config = lib.mkMerge [
    (lib.mkIf enabled {
      envy.software.nix.packages.include = with pkgs; [ tilix ];
      envy.software.nix.packages.references.tilix = "nix:tilix";

      dconf.settings = {
        "com/gexperts/Tilix" = {
          default-profile = profile-uuid;
        };

        "com/gexperts/Tilix/profiles/${profile-uuid}" = {
          use-system-font = false;
          font = "Maple Mono NF CN 12";
          visible-name = "Default (Tokyo Night)";
          background-color = palette.background;
          foreground-color = palette.foreground;
          cursor-background-color = palette.foreground;
          palette = palette.ansi;
          use-theme-colors = false;
        };
      };
    })
    (lib.mkIf alacrittySelected {
      programs.alacritty = {
        enable = true;
        package = null;
        settings = {
          general.import = [
            "${pkgs.vimPlugins.tokyonight-nvim.src}/extras/alacritty/tokyonight_night.toml"
          ];
          font.normal.family = "Maple Mono NF CN";
          font.size = 12.0;
        };
      };
    })
  ];
}
