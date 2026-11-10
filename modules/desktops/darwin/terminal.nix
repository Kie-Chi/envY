{ config, pkgs, lib, ... }:

let
  palette = import ../../cores/tokyo-night.nix;
  itermColor = hex: lib.listToAttrs (lib.imap0 (index: name: {
    name = "${name} Component";
    value = (lib.fromHexString (builtins.substring (1 + index * 2) 2 hex)) / 255.0;
  }) [ "Red" "Green" "Blue" ]);
  terminalColors = {
    "Background Color" = itermColor palette.background;
    "Foreground Color" = itermColor palette.foreground;
    "Cursor Color" = itermColor palette.foreground;
  } // lib.listToAttrs (lib.imap0 (index: hex: {
    name = "Ansi ${toString index} Color";
    value = itermColor hex;
  }) palette.ansi);
  terminalScratchpadGesture = config.envy.habits.terminalScratchpad.gesture;
  quakeHotkeys = {
    F2 = { keyCode = 120; character = 63237; };
    F3 = { keyCode = 99; character = 63238; };
    F4 = { keyCode = 118; character = 63239; };
    F5 = { keyCode = 96; character = 63240; };
    F6 = { keyCode = 97; character = 63241; };
    F7 = { keyCode = 98; character = 63242; };
    F8 = { keyCode = 100; character = 63243; };
    F9 = { keyCode = 101; character = 63244; };
    F10 = { keyCode = 109; character = 63245; };
    F12 = { keyCode = 111; character = 63247; };
  };
  quakeHotkey = quakeHotkeys.${terminalScratchpadGesture} // {
    binding = terminalScratchpadGesture;
    modifier = 8388608;
  };
in
{
  envy.darwin.software.nix.fonts.include = with pkgs; [
    maple-mono.NF-CN
    noto-fonts-cjk-sans
    noto-fonts-color-emoji
  ];

  envy.darwin.software.nix.fonts.references = {
    "MapleMono-NF-CN" = "nix:maple-mono.NF-CN";
    "noto-fonts-cjk-sans" = "nix:noto-fonts-cjk-sans";
    "noto-fonts-color-emoji" = "nix:noto-fonts-color-emoji";
  };

  envy.darwin.software.homebrew.casks.include = [ "iterm2" ];

  envy.machine.habits = lib.optional (
    builtins.elem "iterm2" config.envy.darwin.software.homebrew.casks.effective
  ) {
    id = "terminal-scratchpad";
    label = "Terminal scratchpad";
    gesture = quakeHotkey.binding;
    semantic = "Toggle a reusable Quake-style terminal overlay.";
    context = "darwin";
    backend = "iTerm2 Quake Hotkey Window";
    binding = quakeHotkey.binding;
    ownership = "declarative";
    note = "The Quake profile is a floating full-width iTerm2 hotkey window.";
    requirements = [
      {
        group = "homebrew.system.cask";
        item = "iterm2";
      }
    ];
  };

  # Let OSC 52 copies from terminal programs, including amux sessions, reach
  # the macOS clipboard.
  system.defaults.CustomUserPreferences = lib.mkIf (
    builtins.elem "iterm2" config.envy.darwin.software.homebrew.casks.effective
  ) {
    "com.googlecode.iterm2".AllowClipboardAccess = true;
  };

  home-manager.users."${config.envy.user.name}" = lib.mkIf (
    builtins.elem "iterm2" config.envy.darwin.software.homebrew.casks.effective
  ) {
    home.file."iterm2-tokyo-night-profile" = {
      target = "Library/Application Support/iTerm2/DynamicProfiles/TokyoNight.json";
      text = builtins.toJSON {
        Profiles = [
          ({
            Name = "Tokyo Night";
            Guid = "D2AC4B52-E7A4-4A4D-B9B2-9654271A9144";
            # Use the same dark profile for ordinary iTerm2 launches as for
            # the F12 Quake window. Without this flag iTerm2 keeps its
            # built-in light "Default" bookmark for regular windows.
            "Default Bookmark" = true;
            "Custom Command" = "No";

            "Normal Font" = "MapleMono-NF-CN-Regular 13";
            "Non Ascii Font" = "Monaco 12";
            "Use Ligatures" = true;

            "Cursor Type" = 2;
            "Transparency" = 0.15;
            "Blur" = true;
            "Blur Radius" = 20;

          } // terminalColors)
          ({
            Name = "Quake";
            Guid = "E5926B63-798B-4B6B-B6B1-D181D39D377B";
            "Custom Command" = "No";

            "Normal Font" = "MapleMono-NF-CN-Regular 13";
            "Non Ascii Font" = "Monaco 12";
            "Use Ligatures" = true;

            "Window Type" = 1;
            "Screen" = -2; # Follow the screen containing the mouse cursor.
            "Space" = -1;

            "Transparency" = 0.0;
            "Blur" = false;
            "Horizontal Canvas Fraction" = 1.0;
            "Rows" = 25;

            "Has Hotkey" = true;
            "HotKey Activated By Modifier" = false;
            "HotKey Key Code" = quakeHotkey.keyCode;            # F12 的硬件码
            "HotKey Character" = quakeHotkey.character;         # F12 的 Unicode 字符码
            "HotKey Character (Native)" = quakeHotkey.character;
            "HotKey Modifier" = quakeHotkey.modifier;           # Function 键掩码
          } // terminalColors)
          {
            Name = "BtopDashboard";
            Guid = "F6A1C2D3-E4B5-4C6D-A7B8-D9E0F1A2B3C4";
            "Custom Command" = "Yes";
            "Command" = "${pkgs.btop}/bin/btop";
            "Window Type" = 2;
            "Has Hotkey" = true;
            "HotKey Key Code" = 51;
            "HotKey Character" = 63246;
            "HotKey Character (Native)" = 63246;
            "HotKey Modifier" = 8388608;
            "HotKey Modifier Flags" = 393216;
            "Floating window" = true;
            "Animate showing and hiding" = true;
            "Space" = -1;
            "Rows" = 30;
            "Columns" = 100;
          }
        ];
      };
    };
  };
}
