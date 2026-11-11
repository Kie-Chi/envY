{ config, lib, pkgs, ... }:

let
  palette = import ./tokyo-night.nix;
  colors = [
    palette.background
    (builtins.elemAt palette.ansi 8)
    (builtins.elemAt palette.ansi 7)
    (builtins.elemAt palette.ansi 4)
    (builtins.elemAt palette.ansi 3)
    palette.background
    palette.foreground
    palette.background
    (builtins.elemAt palette.ansi 3)
    (builtins.elemAt palette.ansi 5)
    (builtins.elemAt palette.ansi 2)
    palette.foreground
    palette.foreground
    palette.background
    palette.background
    (builtins.elemAt palette.ansi 1)
    palette.foreground
  ];
  themeColors = lib.concatStringsSep "\n" (lib.imap0
    (index: color: ''tmux_conf_theme_colour_${toString (index + 1)}="${color}"'')
    colors);
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
      # Keep its status formats and bindings while normalizing terminal I/O.
      ${themeColors}
      tmux_conf_theme_status_left_fg="${palette.foreground}"
      tmux_conf_theme_status_left_bg="${palette.background}"
      tmux_conf_theme_status_right_fg="${palette.foreground}"
      tmux_conf_theme_status_right_bg="${palette.background}"
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
      # Let applications request modified keys on every terminal and reconnect.
      set -s extended-keys on #!important
      set -s extended-keys-format csi-u #!important
      set -g set-clipboard on
      set -g allow-passthrough on
      set -g status-keys emacs
      set -g mode-keys vi
      set -as terminal-features ',xterm*:RGB:clipboard'

      # Refresh Wayland clipboard context without replacing SSH/X11 defaults.
      # Check membership so repeated reloads do not append duplicate entries.
      if -F '#{!:#{m/r:(^| )WAYLAND_DISPLAY( |$),#{update-environment}}}' 'set -ag update-environment WAYLAND_DISPLAY'
      if -F '#{!:#{m/r:(^| )XDG_SESSION_TYPE( |$),#{update-environment}}}' 'set -ag update-environment XDG_SESSION_TYPE'

      # Keep Ctrl-a and Ctrl-l available to Vim and shell applications.
      set -g prefix2 None #!important
      unbind C-a #!important
      unbind -n C-l #!important
    '';
  };
}
