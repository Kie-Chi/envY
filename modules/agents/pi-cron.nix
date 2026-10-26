{ config, lib, pkgs, ... }:

let
  home = config.home.homeDirectory;
  agentDir = "${home}/.pi/agent";
  cronExtension = import ./pi-npm-extension.nix { inherit pkgs; } {
    pname = "pi-extension-cron";
    version = "0.5.0";
    url = "https://registry.npmjs.org/@ryan_nookpi/pi-extension-cron/-/pi-extension-cron-0.5.0.tgz";
    hash = "sha512-vk2f8HWppOLYJ3Mq065UjCCTQvXSXw1iH4Xa/DWepSA8kyFYfM2T9ro3QkLRnNh0MXRJX3rCDkZ81SaguZ1kzg==";
  };
  # launchd does not expand shell variables or read the login shell profile.
  sessionPath = map (lib.replaceStrings [ "$HOME" ] [ home ]) config.home.sessionPath;
in
{
  home.file.".pi/agent/extensions/envy-cron".source = cronExtension;

  # Own the daemon's LaunchAgent so it references stable Nix paths. The
  # extension detects this loaded label and does not write its own plist.
  # Jobs, prompts, run logs, and daemon state remain under ~/.pi/agent/cron.
  launchd.agents.pi-cron = {
    enable = true;
    config = {
      Label = "dev.pi.cron";
      ProgramArguments = [ (lib.getExe pkgs.nodejs_22) "${cronExtension}/daemon.mjs" ];
      WorkingDirectory = agentDir;
      RunAtLoad = true;
      KeepAlive = true;
      StandardOutPath = "${agentDir}/cron/daemon.log";
      StandardErrorPath = "${agentDir}/cron/daemon.err.log";
      EnvironmentVariables = {
        PATH = lib.concatStringsSep ":" (sessionPath ++ [
          "${config.home.profileDirectory}/bin"
          "/run/current-system/sw/bin"
          "/nix/var/nix/profiles/default/bin"
          "/opt/homebrew/bin"
          "/usr/local/bin"
          "/usr/bin"
          "/bin"
          "/usr/sbin"
          "/sbin"
        ]);
        PI_CODING_AGENT_DIR = agentDir;
        # Scheduled runs use the envY wrapper for credentials and model policy.
        PI_CRON_PI_BIN = "${config.home.profileDirectory}/bin/pi";
      };
    };
  };

  # launchd cannot open log files in a missing directory.
  home.activation.piCronStateDirectory = lib.hm.dag.entryBetween [ "setupLaunchAgents" ] [ "writeBoundary" ] ''
    run mkdir -p "${agentDir}/cron"
  '';
}
