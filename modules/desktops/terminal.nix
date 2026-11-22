{ config, lib, machinePlatform, osConfig ? { }, ... }:

let
  palette = import ../cores/tokyo-night.nix;
  selected = if machinePlatform == "darwin" then
    # cmux embeds Ghostty and reads the same terminal configuration.
    lib.any (name: builtins.elem name (osConfig.envy.darwin.software.homebrew.casks.effective or [ ]))
      [ "ghostty" "cmux" ]
  else
    builtins.elem "ghostty" (map lib.getName config.envy.software.nix.packages.effective);
in
{
  programs.ghostty = {
    enable = selected;
    package = null;
    systemd.enable = false;
    enableBashIntegration = false;
    enableFishIntegration = false;
    enableZshIntegration = false;
    settings = {
      font-family = "Maple Mono NF CN";
      font-size = 13;
      background = palette.background;
      foreground = palette.foreground;
      cursor-color = palette.foreground;
      palette = lib.imap0 (index: color: "${toString index}=${color}") palette.ansi;
    };
  };
}
