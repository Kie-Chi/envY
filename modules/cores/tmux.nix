{ config, lib, pkgs, ... }:

let
  tmuxSelected = !(builtins.elem "tmux" config.envy.software.nix.packages.exclude);
  ohMyTmux = pkgs.fetchFromGitHub {
    owner = "gpakosz";
    repo = ".tmux";
    rev = "58a3dcc0d718ec0fa1c0d5a2fddd640a1ad7a5b7";
    hash = "sha256-YDzbZVFhrAMBhOI8HVDxR3rEPkrEwG57IUbnZicmfn4=";
  };
in
{
  config = lib.mkIf tmuxSelected {
    envy.software.nix.packages.include = [ pkgs.perl ];
    envy.software.nix.packages.references.perl = "nix:perl";

    home.file.".tmux.conf".source = ohMyTmux + "/.tmux.conf";
    home.file.".tmux.conf.local".text = ''
      # Oh My Tmux customizations managed by envY.
      tmux_conf_new_session_retain_current_path=true
      tmux_conf_new_window_retain_current_path=true
      tmux_conf_new_pane_retain_current_path=true
      tmux_conf_copy_to_os_clipboard=true
      tmux_conf_update_plugins_on_launch=false
      tmux_conf_update_plugins_on_reload=false
      tmux_conf_theme_status_left=" #S | #{pretty_pane_current_path} "
      tmux_conf_theme_status_right=" #{prefix}#{mouse}#{synchronized} | %F %R "

      set -g history-limit 50000
      set -g mouse on
      set -g focus-events on
      set -g default-terminal tmux-256color #!important
      set -g set-clipboard on
      set -g allow-passthrough on
      set -g status-keys emacs
      set -g mode-keys vi
      set -as terminal-features ',xterm*:RGB:clipboard'

      # Keep Ctrl-a and Ctrl-l available to Vim and shell applications.
      set -g prefix2 None #!important
      unbind C-a #!important
      unbind -n C-l #!important
    '';
  };
}
