{ config, lib, ... }:

let
  keyRepeat = config.system.defaults.NSGlobalDomain.KeyRepeat or null;
  fnState = config.system.defaults.NSGlobalDomain."com.apple.keyboard.fnState" or null;
in
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

  envy.machine.habits = lib.optional (keyRepeat != null) {
    kind = "preference";
    id = "keyboard-repeat";
    label = "Keyboard repeat rate";
    semantic = "Store the keyboard repeat interval preference.";
    context = "darwin";
    backend = "defaults";
    binding = "NSGlobalDomain.KeyRepeat";
    ownership = "declarative";
    note = "Readback verifies the stored preference; individual applications may use different repeat behavior.";
    readback = {
      adapter = "darwin.key-repeat";
      expected = keyRepeat;
    };
  } ++ lib.optional (fnState != null) {
    kind = "preference";
    id = "function-key-mode";
    label = "Function key mode";
    semantic = "Store whether the top-row keys act as standard function keys.";
    context = "darwin";
    backend = "defaults";
    binding = "NSGlobalDomain.com.apple.keyboard.fnState";
    ownership = "declarative";
    note = "Readback verifies the stored preference; keyboard drivers and individual applications may handle function keys differently.";
    readback = {
      adapter = "darwin.fn-state";
      expected = fnState;
    };
  };

  programs.zsh.enable = true;
  # Home Manager's oh-my-zsh runs compinit with the final fpath. A second,
  # global call with a different fpath rebuilds the dump on every shell start.
  programs.zsh.enableGlobalCompInit = false;
}
