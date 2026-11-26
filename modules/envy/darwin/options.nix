{ lib, ... }:

let
  inherit (lib) mkOption types;
  inherit (import ../selection-options.nix { inherit lib; }) packageSelection stringSelection;
in
{
  options.envy.darwin = {
    services = {
      mihomo = {
        mode = mkOption {
          type = types.enum [ "none" "manual" "keep" ];
          default = "none";
          description = "Mihomo service mode: disabled, manually controlled, or kept running.";
        };
        tun = mkOption {
          type = types.bool;
          default = false;
          description = "Whether Mihomo uses TUN mode.";
        };
      };

      openssh.mode = mkOption {
        type = types.enum [ "none" "manual" "keep" ];
        default = "manual";
        description = "OpenSSH service mode: disabled, manually controlled by macOS, or kept enabled.";
      };
    };

    software = {
      nix = {
        systemPackages = packageSelection "Darwin system packages";
        fonts = packageSelection "Darwin font packages";
      };
      homebrew = {
        formulae = stringSelection "Darwin Homebrew formulae";
        casks = stringSelection "Darwin Homebrew casks";
        repositories = stringSelection "Darwin Homebrew repositories";
        uninstall = lib.genAttrs [ "formulae" "casks" ] (kind: mkOption {
          type = types.listOf (types.strMatching "[a-z0-9][a-z0-9+_.@-]*(/[a-z0-9][a-z0-9+_.@-]*/[a-z0-9][a-z0-9+_.@-]*)?");
          default = [ ];
          description = "Installed Homebrew ${kind} to explicitly uninstall during activation, without zap or autoremove. Conflicts with final installation selections are rejected.";
        });
      };
    };
  };
}
