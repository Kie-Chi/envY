{ config, lib, pkgs, machinePlatform, ... }:

let
  policy = config.envy.storage;
  organize = pkgs.callPackage ./organize.nix { };
  tools = [ organize pkgs.restic ] ++ lib.optional (machinePlatform == "linux") pkgs.lsof;
  executable = "${config.home.profileDirectory}/bin/envy";
  command = [ executable "storage" "run" "--scheduled" ];
  runtimePath = lib.concatStringsSep ":" [
    (lib.makeBinPath (tools ++ [ pkgs.openssh ]))
    "${config.home.profileDirectory}/bin"
    "/usr/bin" "/bin" "/usr/sbin" "/sbin"
  ];
in
{
  envy.software.nix.packages = {
    include = tools;
    references = {
      ${lib.getName organize} = "local:modules/storage/organize.nix";
      ${lib.getName pkgs.restic} = "nix:restic";
    } // lib.optionalAttrs (machinePlatform == "linux") {
      ${lib.getName pkgs.lsof} = "nix:lsof";
    };
  };

  sops.secrets = lib.optionalAttrs (policy.archive.repository != "") {
    storage-archive-password = {
      sopsFile = ../../secrets/secrets.yaml;
      key = "storage/archive/password";
    };
  };

  launchd.agents.envy-storage = lib.mkIf (machinePlatform == "darwin" && policy.mode != "manual") {
    enable = true;
    config = {
      Label = "dev.envy.storage";
      ProgramArguments = command;
      StartCalendarInterval = [ { Hour = 3; Minute = 0; } ];
      EnvironmentVariables.PATH = runtimePath;
      WorkingDirectory = config.home.homeDirectory;
    };
  };

  systemd.user = lib.mkIf (machinePlatform == "linux" && policy.mode != "manual") {
    services.envy-storage = {
      Unit.Description = "envY storage lifecycle";
      Service = {
        Type = "oneshot";
        ExecStart = lib.escapeShellArgs command;
        Environment = [ "PATH=${runtimePath}" ];
        WorkingDirectory = config.home.homeDirectory;
      };
    };
    timers.envy-storage = {
      Unit.Description = "Daily envY storage lifecycle";
      Timer = {
        OnCalendar = "*-*-* 03:00:00";
        Persistent = true;
      };
      Install.WantedBy = [ "timers.target" ];
    };
  };
}
