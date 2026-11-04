{ config, lib, pkgs, sys, machinePlatform, ... }:

let
  catalog = import ./catalog.nix;
  profile = (import ./resolve.nix { inherit lib; })
    catalog.${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  substituters = lib.concatStringsSep " " profile.nix.substituters;
  extraSubstituters = lib.concatStringsSep " " profile.nix.extraSubstituters;
  nixTrustSource = ../../resources/scripts/nix-trust.sh;
  nixTrust = pkgs.writeShellApplication {
    name = "envy-nix-trust";
    runtimeInputs = with pkgs; [ coreutils diffutils gawk gnugrep ];
    text = ''
      exec ${pkgs.bash}/bin/bash "${nixTrustSource}" "$@"
    '';
  };
  nixTrustCommand = "${nixTrust}/bin/envy-nix-trust";
  condaConfig = {
    envs_dirs = [ "~/.mamba/envs" ];
    pkgs_dirs = [ "~/.mamba/pkgs" ];
    channels = [ profile.conda.condaForge "defaults" ];
    show_channel_urls = true;
    default_channels = profile.conda.defaultChannels;
  };
  nixMirrorConfig = ''
    substituters = ${substituters}
    ${lib.optionalString (profile.nix.extraSubstituters != [ ]) "extra-substituters = ${extraSubstituters}"}
  '';
in
{
  imports = lib.optionals (machinePlatform == "linux") [ ./linux.nix ];

  # nix.enable is false on Darwin so nix-darwin does not own the installer's
  # daemon configuration. Use the same marked configuration on both platforms.
  home.activation.configureNixDaemonTrust = sys.task.activation {
    name = "configure-nix-daemon-trust";
    script = ''
      if ${nixTrustCommand} status \
          --mode "${config.envy.mirrors.mode}" \
          --user "${config.envy.user.name}"; then
        :
      else
        TRUST_STATUS=$?
        if [ "$TRUST_STATUS" -ne 1 ]; then
          exit "$TRUST_STATUS"
        fi
        esudo ${nixTrustCommand} repair \
          --mode "${config.envy.mirrors.mode}" \
          --user "${config.envy.user.name}" \
          --elevated
      fi
    '';
  };

  home.sessionVariables = import ./env.nix profile;

  home.file = {
    ".config/nix/nix.conf".text = ''
      extra-experimental-features = nix-command flakes
      ${nixMirrorConfig}fallback = true
      connect-timeout = 5
    '';
  } // lib.optionalAttrs (machinePlatform == "linux") {
    ".condarc".text = lib.generators.toYAML { } condaConfig;
  } // lib.optionalAttrs (
    machinePlatform == "linux" && config.envy.mirrors.mode == "china"
  ) {
    ".m2/settings.xml".text = ''
      <?xml version="1.0" encoding="UTF-8"?>
      <settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">
        <mirrors>
          <mirror>
            <id>envy-china</id>
            <name>envY China Maven mirror</name>
            <url>${profile.maven.repository}</url>
            <mirrorOf>central</mirrorOf>
          </mirror>
        </mirrors>
      </settings>
    '';
  };
}
