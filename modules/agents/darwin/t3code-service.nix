{ config, lib, t3codeService, ... }:
{
  config = lib.mkIf t3codeService.selected {
    launchd.agents.t3code = {
      enable = true;
      config = {
        Label = "com.t3tools.t3code.service";
        ProgramArguments = [ (toString t3codeService.runner) ];
        EnvironmentVariables = t3codeService.environment // {
          T3_BOOT_SERVICE_UNIT = "com.t3tools.t3code.service.plist";
        };
        WorkingDirectory = config.home.homeDirectory;
        RunAtLoad = true;
        KeepAlive = true;
        ThrottleInterval = 10;
        ExitTimeOut = 60;
        StandardOutPath = "${t3codeService.logs}/service.log";
        StandardErrorPath = "${t3codeService.logs}/service.err.log";
      };
    };
    home.activation.retireLegacyT3CodeService = lib.hm.dag.entryBetween
      [ "setupLaunchAgents" ] [ "prepareT3CodeService" ] ''
        legacy=${lib.escapeShellArg "${config.home.homeDirectory}/Library/LaunchAgents/org.envy.t3code.plist"}
        if [ -f "$legacy" ]; then
          legacyLabel="$(/usr/libexec/PlistBuddy -c "Print :Label" "$legacy")"
          legacyCommand="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$legacy")"
          if [ "$legacyLabel" != "org.envy.t3code" ] || [[ "$legacyCommand" != *"/nix/store/"*"-envy-t3code-service"* ]]; then
            echo "Refusing to retire an unrecognized legacy T3 Code service." >&2
            exit 1
          fi
          if [ -z "$DRY_RUN_CMD" ]; then
            if /bin/launchctl print "gui/$UID/org.envy.t3code" >/dev/null 2>&1; then
              /bin/launchctl bootout --wait "gui/$UID/org.envy.t3code"
            fi
            /bin/rm -f "$legacy"
          fi
        fi
      '';
  };
}
