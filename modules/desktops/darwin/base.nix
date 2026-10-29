{ config, ... }:
{
  system.stateVersion = 6;
  system.primaryUser = config.envy.user.name;
  nixpkgs.config.allowUnfree = true;
  nix.enable = false;
  system.defaults = {
    dock = {
      autohide = true;
      show-recents = false;
    };
    finder.AppleShowAllExtensions = true;
    NSGlobalDomain = {
      "com.apple.mouse.tapBehavior" = 1;
      KeyRepeat = 2;
    };
  };
  system.defaults.NSGlobalDomain."com.apple.keyboard.fnState" = true;

  programs.zsh.enable = true;
  # Home Manager's oh-my-zsh runs compinit with the final fpath. A second,
  # global call with a different fpath rebuilds the dump on every shell start.
  programs.zsh.enableGlobalCompInit = false;
}
