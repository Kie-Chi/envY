{ pkgs, config, lib, sys, nixGLDefault ? null, ... }:

let
  isDesktop = config.envy.linux.option == "desktop";
  enabled = isDesktop && !(builtins.elem "sunshine" config.envy.software.nix.packages.exclude);
  gnomeIntegration = config.envy.linux.desktop == "gnome"
    || config.envy.linux.desktop == "all";
  sunshineAutostartDesktop = pkgs.runCommand "sunshine-autostart-desktop" {} ''
    mkdir -p $out/share/applications
    cp ${../../../files/remote/sunshine.desktop} $out/share/applications/sunshine.desktop
  '';
  sunshineExec = "${lib.optionalString (nixGLDefault != null) "${nixGLDefault}/bin/nixGL "}${pkgs.sunshine}/bin/sunshine";
in
{
  config = lib.mkMerge [
    (lib.mkIf isDesktop {
      envy.software.nix.packages.include = [ pkgs.sunshine ];
      envy.software.nix.packages.references.sunshine = "nix:sunshine";
    })
    (lib.mkIf enabled {
  systemd.user.services.sunshine = {
    Unit = {
      Description = "Sunshine Game Stream Host";
      After = [ "graphical-session.target" ];
      Wants = [ "graphical-session.target" ];
    };

    Service = {
      ExecStart = sunshineExec;
      Environment = [
        "DISPLAY=:0"
        "XAUTHORITY=%h/.Xauthority"
      ];

      Restart = "always";
      RestartSec = 5;
    };

    Install = {
      WantedBy = [ "graphical-session.target" ];
    };
  };

  programs.gnome-shell = lib.mkIf gnomeIntegration {
    enable = true;
    extensions = [
      { package = pkgs.gnomeExtensions.sunshinestatus; }
    ];
  };

  xdg.autostart.entries = [
    sunshineAutostartDesktop
  ];

  home.file.".mac_screen".text = ''
    add 2560 1664 60
    switch 2560x1664_60_User
    scale 200
    text-scale 1.1
  '';
  home.file.".no_screen".text = ''
    text-scale 1.0
  '';

  home.file.".screen".source = config.lib.file.mkOutOfStoreSymlink "${config.home.homeDirectory}/.mac_screen";

  home.activation.setupSunshineInput = sys.task.activation {
    name = "setupSunshineInput";
    message = "Setting up Sunshine uinput permissions...";
    script = ''
      ${sys.deploy {
        name = "85-sunshine-input.rules";
        format = "lines";
        data = [
          ''KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"''
          ''KERNEL=="uhid", GROUP="input", MODE="0660", OPTIONS+="static_node=uhid"''
        ];
        target = "/etc/udev/rules.d/85-sunshine-input.rules";
        mode = "0644";
      }}

      ${sys.deploy {
        name = "uinput.conf";
        format = "lines";
        data = [ "uinput" "uhid" ];
        target = "/etc/modules-load.d/uinput.conf";
        mode = "0644";
      }}

      if ! id -nG "${config.home.username}" | ${sys.cmds.grep} -qw input; then
        esudo ${sys.cmds.usermod} -aG input ${config.home.username}
      fi

      # enable the input
      esudo ${sys.cmds.modprobe} uinput
      esudo ${sys.cmds.modprobe} uhid
      esudo ${sys.cmds.udevadm} control --reload-rules
      esudo ${sys.cmds.udevadm} trigger

      esudo ${sys.cmds.ufw} allow 47984,47989,47990,48010/tcp
      esudo ${sys.cmds.ufw} allow 47998:48000/udp
      esudo ${sys.cmds.ufw} allow 48002/udp
      esudo ${sys.cmds.ufw} allow 48010/udp
      esudo ${sys.cmds.ufw} reload
    '';
  };
    })
  ];
}
