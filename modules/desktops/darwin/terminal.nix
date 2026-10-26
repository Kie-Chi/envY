{ config, pkgs, lib, ... }:

let
  dracula-colors = pkgs.fetchurl {
    url = "https://raw.githubusercontent.com/dracula/iterm/master/Dracula.itermcolors";
    sha256 = "sha256-X+/B1lmyAggD5mIk0zNkuFPsUqVNIJ71PD18niEn/SA=";
  };
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
    home.file."iterm2-dracula-profile" = {
      target = "Library/Application Support/iTerm2/DynamicProfiles/Dracula.json";
      text = builtins.toJSON {
        Profiles = [
          {
            Name = "Dracula";
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

            "Background Color" = { "Red Component" = 0.1568; "Green Component" = 0.1647; "Blue Component" = 0.2117; };
            "Foreground Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; };
            "Cursor Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; };
            "Ansi 0 Color" = { "Red Component" = 0.1568; "Green Component" = 0.1647; "Blue Component" = 0.2117; }; # Black
            "Ansi 1 Color" = { "Red Component" = 1.0; "Green Component" = 0.3333; "Blue Component" = 0.3333; }; # Red
            "Ansi 2 Color" = { "Red Component" = 0.3137; "Green Component" = 0.9804; "Blue Component" = 0.4823; }; # Green
            "Ansi 3 Color" = { "Red Component" = 0.9451; "Green Component" = 0.9804; "Blue Component" = 0.5490; }; # Yellow
            "Ansi 4 Color" = { "Red Component" = 0.7412; "Green Component" = 0.5765; "Blue Component" = 0.9765; }; # Blue
            "Ansi 5 Color" = { "Red Component" = 1.0; "Green Component" = 0.4745; "Blue Component" = 0.7765; }; # Magenta
            "Ansi 6 Color" = { "Red Component" = 0.5451; "Green Component" = 0.9137; "Blue Component" = 0.9921; }; # Cyan
            "Ansi 7 Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; }; # White
          }
          {
            Name = "Quake";
            Guid = "E5926B63-798B-4B6B-B6B1-D181D39D377B";
            "Custom Command" = "No";

            "Normal Font" = "MapleMono-NF-CN-Regular 13";
            "Non Ascii Font" = "Monaco 12";
            "Use Ligatures" = true;

            "Window Type" = 1;
            "Screen" = -2; # Follow the screen containing the mouse cursor.
            "Space" = -1;

            "Transparency" = 0.25;
            "Blur" = true;
            "Blur Radius" = 25;
            "Horizontal Canvas Fraction" = 1.0;
            "Rows" = 25;

            "Background Color" = { "Red Component" = 0.1568; "Green Component" = 0.1647; "Blue Component" = 0.2117; };
            "Foreground Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; };
            "Cursor Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; };
            "Ansi 0 Color" = { "Red Component" = 0.1568; "Green Component" = 0.1647; "Blue Component" = 0.2117; }; # Black
            "Ansi 1 Color" = { "Red Component" = 1.0; "Green Component" = 0.3333; "Blue Component" = 0.3333; }; # Red
            "Ansi 2 Color" = { "Red Component" = 0.3137; "Green Component" = 0.9804; "Blue Component" = 0.4823; }; # Green
            "Ansi 3 Color" = { "Red Component" = 0.9451; "Green Component" = 0.9804; "Blue Component" = 0.5490; }; # Yellow
            "Ansi 4 Color" = { "Red Component" = 0.7412; "Green Component" = 0.5765; "Blue Component" = 0.9765; }; # Blue
            "Ansi 5 Color" = { "Red Component" = 1.0; "Green Component" = 0.4745; "Blue Component" = 0.7765; }; # Magenta
            "Ansi 6 Color" = { "Red Component" = 0.5451; "Green Component" = 0.9137; "Blue Component" = 0.9921; }; # Cyan
            "Ansi 7 Color" = { "Red Component" = 0.9725; "Green Component" = 0.9725; "Blue Component" = 0.9490; }; # White
            "Has Hotkey" = true;
            "HotKey Activated By Modifier" = false;
            "HotKey Key Code" = quakeHotkey.keyCode;            # F12 的硬件码
            "HotKey Character" = quakeHotkey.character;         # F12 的 Unicode 字符码
            "HotKey Character (Native)" = quakeHotkey.character;
            "HotKey Modifier" = quakeHotkey.modifier;           # Function 键掩码
          }
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
