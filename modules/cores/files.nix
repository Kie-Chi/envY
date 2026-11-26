{ config, pkgs, lib, ... }:

let
  yaziSelected = builtins.elem "yazi" (map lib.getName config.envy.software.nix.packages.effective);
in
{
  envy.software.nix.packages.include = [ pkgs.yazi ];
  envy.software.nix.packages.references.yazi = "nix:yazi";

  programs.yazi = {
    enable = yaziSelected;
    package = null;
    shellWrapperName = "yy";
    enableBashIntegration = false;
    enableFishIntegration = false;
    enableNushellIntegration = false;
    enableZshIntegration = true;
  };
  xdg.configFile."yazi/theme.toml" = lib.mkIf yaziSelected {
    # The locked Yazi expects url patterns; the upstream theme still uses name.
    text = builtins.replaceStrings [ "{ name =" ] [ "{ url =" ]
      (builtins.readFile "${pkgs.vimPlugins.tokyonight-nvim.src}/extras/yazi/tokyonight_night.toml");
  };
}
