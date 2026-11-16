# {...}:{}
{ config, pkgs, lib, sys, nixGLDefault ? null, ... }:

let
  selected = config.envy.linux.option == "desktop"
    && (config.envy.linux.desktop == "niri" || config.envy.linux.desktop == "all");
  enabled = selected && !(builtins.elem "niri" config.envy.software.nix.packages.exclude);
  wallpaperPath = ../../../resources/images/background.jpg;
  screenshotHelper = pkgs.writeShellApplication {
    name = "screenshot";
    runtimeInputs = with pkgs; [ grim slurp swappy ];
    text = builtins.readFile ../../../resources/helpers/screenshot;
  };
  fixPipewireHelper = pkgs.writeShellApplication {
    name = "fix-pipewire";
    runtimeInputs = [ pkgs.systemd ];
    text = builtins.readFile ../../../resources/helpers/fix-pipewire;
  };
  niri-wrapper = pkgs.writeShellScriptBin "start-niri-desktop" ''
    export XDG_SESSION_TYPE=wayland
    export XDG_CURRENT_DESKTOP=niri
    export XDG_SESSION_DESKTOP=niri
    export MOZ_ENABLE_WAYLAND=1
    export QT_QPA_PLATFORM="wayland;xcb"
    [ -f /etc/profile ] && . /etc/profile
    [ -f "$HOME/.profile" ] && . "$HOME/.profile"
    [ -f "$HOME/.bash_profile" ] && . "$HOME/.bash_profile"
    if [ -f "${config.home.profileDirectory}/etc/profile.d/hm-session-vars.sh" ]; then
      . "${config.home.profileDirectory}/etc/profile.d/hm-session-vars.sh"
    fi
    ${pkgs.systemd}/bin/systemctl --user import-environment PATH XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP MOZ_ENABLE_WAYLAND
    ${pkgs.dbus}/bin/dbus-update-activation-environment --systemd --all
    exec ${lib.optionalString (nixGLDefault != null) "${nixGLDefault}/bin/nixGL"} ${pkgs.niri}/bin/niri > "$HOME/niri.log" 2>&1
  '';
  niriDesktop = [
    {
      format = "plain";
      data = "# Managed by home-manager\n";
    }
    {
      format = "ini";
      data = {
        "Desktop Entry" = {
          Name = "niri";
          Comment = "A scrollable-tiling Wayland compositor";
          Exec = "${niri-wrapper}/bin/start-niri-desktop";
          Type = "Application";
          DesktopNames = "niri";
        };
      };
    }
  ];
  niriPortal = {
    "preferred" = {
      "default" = "gtk;";
      "org.freedesktop.impl.portal.Access" = "gtk;";
      "org.freedesktop.impl.portal.Notification" = "gtk;";
      "org.freedesktop.impl.portal.Secrets" = "gnome-keyring;";
    };
  };
  keyboardLayout = {
    layoutCode = "us";
    nativeExpectedNames = [ "English (US)" ];
    current_idx = 0;
  };
  terminalScratchpad = {
    binding = config.envy.habits.terminalScratchpad.gesture;
    backend = "nscratch + Alacritty quake-term";
    command = "${config.home.profileDirectory}/bin/nscratch -id quake-term -a -m -s '${pkgs.alacritty}/bin/alacritty --class quake-term'";
  };
  globalLauncher = {
    gesture = config.envy.habits.globalLauncher.gesture;
    binding = lib.replaceStrings [ "Option+" ] [ "Super+" ] config.envy.habits.globalLauncher.gesture;
    backend = "Fuzzel";
    command = "${pkgs.fuzzel}/bin/fuzzel";
  };
  generatedNiriConfig = ''

    spawn-at-startup "${pkgs.noctalia-shell}/bin/noctalia-shell"
    spawn-at-startup "${pkgs.xwayland-satellite}/bin/xwayland-satellite"
    spawn-at-startup "${pkgs.swaybg}/bin/swaybg" "-m" "fill" "-i" "${wallpaperPath}"
    spawn-at-startup "/usr/bin/dbus-update-activation-environment" "--all"
    spawn-at-startup "/usr/bin/gnome-keyring-daemon" "--start" "--components=secrets"
    spawn-at-startup "${pkgs.dex}/bin/dex" "-a" "-s" "~/.config/autostart:/etc/xdg/autostart"
    spawn-at-startup "${config.home.profileDirectory}/bin/fcitx5" "-d"
    spawn-at-startup "sh" "-c" "/usr/bin/xrdb -merge ~/.Xresources"
    spawn-at-startup "nemo"

    workspace "desktop"
    workspace "devs"
    workspace "scratch"

    input {
      mod-key "Alt"
      mod-key-nested "Super"
      keyboard {
        xkb {
          layout "${keyboardLayout.layoutCode}"
        }
      }
      touchpad {
        tap
        natural-scroll
      }
    }

    layout {
      gaps 12
      center-focused-column "never"
      default-column-width { proportion 0.5; }

    }

    animations {
    }

    window-rule {
      match title="^noctalia-.*"
      match app-id="^quake-.*"
      open-floating true
      geometry-corner-radius 24
      focus-ring {
        off
      }
      open-focused true
    }

    window-rule {
      match app-id="com.github.hluk.copyq"
      open-floating true
      clip-to-geometry true
    }

    window-rule {
      match app-id="quake-.*|nemo"
      open-floating true
      open-on-workspace "scratch"
      open-focused false
    }

    window-rule {
      match app-id="quake-term"
      open-floating true
      opacity 0.8
      focus-ring {
        off
      }
      default-floating-position x=0 y=0
      default-column-width { proportion 1.0; }
      default-window-height { proportion 1.0; }
      geometry-corner-radius 0 0 12 12
      clip-to-geometry true
    }

    binds {
      "F1" { spawn "${config.home.profileDirectory}/bin/screenshot"; }
      "F11" { spawn-sh "${config.home.profileDirectory}/bin/nscratch -id nemo"; }
      "${terminalScratchpad.binding}" { spawn-sh "${terminalScratchpad.command}"; }
      "Mod+Return" { spawn "${pkgs.alacritty}/bin/alacritty"; }
      "${globalLauncher.binding}" { spawn "${globalLauncher.command}"; }
      "Mod+Q" { close-window; }
      "Mod+Shift+E" { quit; }
      "Mod+V" { spawn "${pkgs.copyq}/bin/copyq" "toggle" ; }

      "Mod+Left"  { focus-column-left; }
      "Mod+Right" { focus-column-right; }
      "Mod+H"     { focus-column-left; }
      "Mod+L"     { focus-column-right; }
      
      "Mod+Shift+Left"  { move-column-left; }
      "Mod+Shift+Right" { move-column-right; }
      "Mod+Shift+H"  { move-column-left; }

      "Mod+Shift+L" { move-column-right; }
      "Mod+Plus"  { set-column-width "+10%"; }
      "Mod+Minus" { set-column-width "-10%"; }
      "Mod+F"     { maximize-column; }
      "Mod+Shift+F" { fullscreen-window; }

      "Mod+1" { focus-workspace 1; }
      "Mod+2" { focus-workspace 2; }
      "Mod+3" { focus-workspace 3; }
      "Mod+Shift+1" { move-column-to-workspace 1; }
      "Mod+Shift+2" { move-column-to-workspace 2; }

      "Mod+Comma" { consume-or-expel-window-left; }
      
      "Mod+Period" { consume-or-expel-window-right; }
      "Mod+Up"   { focus-window-up; }
      "Mod+Down" { focus-window-down; }
      "Mod+J"   { focus-window-up; }
      "Mod+K" { focus-window-down; }
      "Mod+Shift+Up"   { move-window-up; }
      "Mod+Shift+Down" { move-window-down; }
      "Mod+W" { toggle-column-tabbed-display; }

      "XF86AudioRaiseVolume" allow-when-locked=true { spawn-sh "${config.home.profileDirectory}/bin/fix-pipewire && ${pkgs.swayosd}/bin/swayosd-client --output-volume raise"; }
      "XF86AudioLowerVolume" allow-when-locked=true { spawn-sh "${config.home.profileDirectory}/bin/fix-pipewire && ${pkgs.swayosd}/bin/swayosd-client --output-volume lower"; }
      "XF86AudioMute"        allow-when-locked=true { spawn-sh "${config.home.profileDirectory}/bin/fix-pipewire && ${pkgs.swayosd}/bin/swayosd-client --output-volume mute-toggle"; }
      "XF86AudioMicMute"     allow-when-locked=true { spawn-sh "${config.home.profileDirectory}/bin/fix-pipewire && ${pkgs.swayosd}/bin/swayosd-client --input-volume mute-toggle"; }
    }

    output "DP-3" {
      focus-at-startup
      scale 1.5
    }

    output "HDMI-A-1" {
    mode custom=true "3840x2160@60"
    scale 1.5
      focus-at-startup
    }
  '';
  niriConfig = config.xdg.configFile."niri/config.kdl";
  # Match Home Manager's generated source without changing its mkDefault priority.
  generatedNiriSource = pkgs.writeTextFile {
    name = lib.hm.strings.storeFileName "niri/config.kdl";
    text = generatedNiriConfig;
    executable = niriConfig.executable == true;
  };
  ownsKeyboardLayout = niriConfig.enable
    && niriConfig.target == lib.removePrefix (config.home.homeDirectory + "/")
      "${config.xdg.configHome}/niri/config.kdl"
    && niriConfig.text == generatedNiriConfig
    && toString niriConfig.source == toString generatedNiriSource;
in
{
  config = lib.mkMerge [
    (lib.mkIf selected {
      envy.software.nix.packages.include = [ pkgs.niri ];
      envy.software.nix.packages.references.niri = "nix:niri";
    })
    (lib.mkIf enabled {
  envy.software.nix.packages.include = (with pkgs; [
    nemo
    fuzzel
    alacritty
    swaybg
    xwayland-satellite
    wl-clipboard
    dex
    noctalia-shell
  ]) ++ [
    screenshotHelper
    fixPipewireHelper
  ];
  envy.software.nix.packages.references = {
    nemo = "nix:nemo";
    fuzzel = "nix:fuzzel";
    alacritty = "nix:alacritty";
    swaybg = "nix:swaybg";
    "xwayland-satellite" = "nix:xwayland-satellite";
    "wl-clipboard" = "nix:wl-clipboard";
    dex = "nix:dex";
    "noctalia-shell" = "nix:noctalia-shell";
    screenshot = "local:modules/desktops/linux/niri.nix#screenshotHelper";
    "fix-pipewire" = "local:modules/desktops/linux/niri.nix#fixPipewireHelper";
  };

  envy.machine.habits = [
    {
      kind = "preference";
      id = "keyboard-layout";
      label = "Keyboard layout";
      semantic = if ownsKeyboardLayout then "Use the English (US) keyboard layout."
        else "Use the keyboard layout of the active Niri configuration.";
      context = "niri";
      backend = "Niri";
      binding = "input.keyboard.xkb.layout";
      ownership = "declarative";
      note = if ownsKeyboardLayout then
        "Readback verifies the running compositor's layout names and active index; it does not verify key behavior or shortcut bindings."
        else "The generated Niri configuration is overridden, disabled, or redirected; layout identity requires manual verification.";
      readback = if ownsKeyboardLayout then {
        adapter = "linux.niri-keyboard-layout";
        expected = {
          names = keyboardLayout.nativeExpectedNames;
          current_idx = keyboardLayout.current_idx;
        };
      } else null;
      requirements = [
        {
          group = "nix.user.package";
          item = "niri";
        }
      ];
    }
    {
      id = "terminal-scratchpad";
      label = "Terminal scratchpad";
      gesture = terminalScratchpad.binding;
      semantic = "Toggle a reusable Quake-style terminal overlay.";
      context = "niri";
      backend = terminalScratchpad.backend;
      binding = terminalScratchpad.binding;
      ownership = "declarative";
      note = "nscratch reuses quake-term, moves it to the focused monitor, and returns it to the scratch workspace when hidden.";
      requirements = [
        {
          group = "nix.user.package";
          item = "niri-scratchpad";
        }
        {
          group = "nix.user.package";
          item = "alacritty";
        }
      ];
    }
    {
      id = "global-launcher";
      label = "Global launcher";
      gesture = globalLauncher.gesture;
      semantic = "Open the global launcher and search surface.";
      context = "niri";
      backend = globalLauncher.backend;
      binding = globalLauncher.binding;
      ownership = "declarative";
      note = "The Niri binding uses the logical Super key; the contract preserves the user's Option+Space gesture.";
      requirements = [
        {
          group = "nix.user.package";
          item = "fuzzel";
        }
      ];
    }
  ];

  xdg.configFile."niri/config.kdl".text = generatedNiriConfig;
  gtk = {
    enable = true;
    theme = {
      name = "Yaru-Dark";
      package = pkgs.yaru-theme;
    };
    gtk4.theme = config.gtk.theme;
    iconTheme = {
      name = "Yaru";
      package = pkgs.yaru-theme;
    };
  };

  # xdg.configFile."xdg-desktop-portal/niri-portals.conf".source =  
  # "${pkgs.niri}/share/xdg-desktop-portal/niri-portals.conf";

  xdg.configFile."xdg-desktop-portal/niri-portals.conf".text = lib.generators.toINI {} niriPortal;

  services.swayosd.enable = true;

  home.activation.niriStartUp = sys.config.activation {
    name = "niri.desktop";
    format = "concat";
    data = niriDesktop;
    target = "/usr/share/wayland-sessions/niri.desktop";
    mode = "0644";
    post = ''
      esudo_system ${sys.cmds.systemctl} restart nix-daemon
    '';
  };
    })
  ];
}
