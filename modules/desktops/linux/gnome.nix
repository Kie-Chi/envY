{ pkgs, config, lib, ... }:

let
  wallpaperPath = ../../../resources/images/background.jpg;
  desktop = config.envy.linux.desktop;
  enabled = config.envy.linux.option == "desktop"
    && (desktop == "gnome" || desktop == "all");
  quakeHelper = pkgs.writeShellApplication {
    name = "quake";
    runtimeInputs = with pkgs; [ coreutils gnugrep gawk wmctrl xdotool ];
    text = builtins.readFile ../../../resources/helpers/quake;
  };
  tilixQuake = {
    name = "Tilix Quake";
    command = "tilix --quake";
    binding = config.envy.habits.terminalScratchpad.gesture;
  };
in
{
  config = lib.mkIf enabled {
    envy.software.nix.packages.include = (with pkgs; [ wmctrl xdotool ]) ++ [ quakeHelper ];
    envy.software.nix.packages.references = {
      wmctrl = "nix:wmctrl";
      xdotool = "nix:xdotool";
      quake = "local:modules/desktops/linux/gnome.nix#quakeHelper";
    };

    envy.machine.habits = [
      {
        id = "terminal-scratchpad";
        label = "Terminal scratchpad";
        gesture = tilixQuake.binding;
        semantic = "Toggle a reusable Quake-style terminal overlay.";
        context = "gnome";
        backend = tilixQuake.name;
        binding = tilixQuake.binding;
        ownership = "declarative";
        note = "GNOME delegates the overlay lifecycle to Tilix --quake.";
        readback = {
          adapter = "linux.gnome-scratchpad";
          expected = config.dconf.settings."org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake".binding;
        };
        requirements = [
          {
            group = "nix.user.package";
            item = "tilix";
          }
        ];
      }
    ];

    dconf.settings = {
      "org/gnome/desktop/background" = {
        picture-uri = "file://${wallpaperPath}";
        picture-uri-dark = "file://${wallpaperPath}";
        picture-options = "zoom";
      };

      "org/gnome/shell/keybindings" = {
        toggle-message-tray = ["<Alt>v"];
      };
      "org/gnome/shell/extensions/dash-to-dock" = {
        "dock-fixed" = false;
      };

      "org/gnome/settings-daemon/plugins/media-keys" = {
        custom-keybindings = [
          "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake/"
          "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-normal/"
          "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/google-chrome/"
          "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/btop/"
        ];
      };

      "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake" = {
        name = tilixQuake.name;
        command = tilixQuake.command;
        binding = tilixQuake.binding;
      };

      "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-normal" = {
        name = "Tilix";
        command = "tilix";
        binding = "<Control><Alt>t";
      };

      "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/google-chrome" = {
        name = "Chrome";
        command = "google-chrome-stable";
        binding = "F11";
      };

      "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/btop" = {
        name = "Btop";
        command = "quake 'class:QuakeBtop' 'tilix --new-process --class=QuakeBtop --name=QuakeBtop --geometry=120x35 -e btop'";
        binding = "<Control><Alt>Backspace";
      };
    };
  };
}
