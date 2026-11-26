{ config, lib, t3codeService, ... }:
{
  config = lib.mkIf t3codeService.selected {
    systemd.user.startServices = lib.mkDefault "sd-switch";
    systemd.user.services.t3code = {
      Unit = {
        Description = "T3 Code (Nix)";
        Conflicts = [ "envy-t3code.service" ];
        After = [ "envy-t3code.service" ];
      };
      Service = {
        ExecStart = toString t3codeService.runner;
        WorkingDirectory = config.home.homeDirectory;
        Environment = lib.mapAttrsToList (name: value: "${name}=${value}") (t3codeService.environment // { T3_BOOT_SERVICE_UNIT = "t3code.service"; });
        Restart = "always";
        RestartSec = 5;
        TimeoutStopSec = 60;
        KillMode = "mixed";
        UMask = "0077";
      };
      Install.WantedBy = [ "default.target" ];
    };
  };
}
