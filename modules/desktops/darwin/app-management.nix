{ config, lib, pkgs, ... }:

let
  cfg = config.targets.darwin.copyApps;
  appDirectory = "${config.home.homeDirectory}/${cfg.directory}";
in
{
  # Replace the upstream check, which resets all App Management grants on any
  # write failure, including immutable Nix store links left by older generations.
  home.activation.checkAppManagementPermission = lib.mkIf (cfg.enable && cfg.enableChecks) (
    lib.mkForce (lib.hm.dag.entryBefore [ "writeBoundary" ] ''
      run ${lib.getExe pkgs.bash} ${./check-app-management.sh} ${lib.escapeShellArg appDirectory}
    '')
  );
}
