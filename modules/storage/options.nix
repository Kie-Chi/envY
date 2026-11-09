{ config, lib, ... }:

let
  inherit (lib) mkOption types;
  absoluteDirectory = types.addCheck types.str (value: lib.hasPrefix "/" value);
in
{
  options.envy.storage = {
    mode = mkOption {
      type = types.enum [ "manual" "report" "auto" ];
      default = "manual";
      description = "Storage lifecycle execution mode.";
    };
    quarantineDays = mkOption {
      type = types.ints.positive;
      default = 30;
      description = "Minimum quarantine retention in days.";
    };
    protectedPaths = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Paths excluded from storage lifecycle actions.";
    };
    rules = mkOption {
      type = types.listOf (types.submodule {
        options = {
          id = mkOption {
            type = types.strMatching "[a-z][a-z0-9-]*";
            description = "Unique storage rule identifier.";
          };
          path = mkOption {
            type = absoluteDirectory;
            description = "Absolute directory scanned by this rule.";
          };
          action = mkOption {
            type = types.enum [ "quarantine" "archive" ];
            description = "Action for matching files.";
          };
          extensions = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "File extensions to match; empty matches all extensions.";
          };
          minAgeDays = mkOption {
            type = types.ints.positive;
            default = 30;
            description = "Minimum file age in days.";
          };
          minSizeBytes = mkOption {
            type = types.ints.unsigned;
            default = 0;
            description = "Minimum file size in bytes.";
          };
        };
      });
      default = [ ];
      description = "Explicit storage lifecycle rules.";
    };
    archive.repository = mkOption {
      type = types.addCheck types.str (value:
        value == "" || lib.hasPrefix "sftp:" value || lib.hasPrefix "/" value);
      default = "";
      description = "Restic SFTP repository or absolute mounted repository path.";
    };
  };

  config.assertions = [
    {
      assertion = let ids = map (rule: rule.id) config.envy.storage.rules;
        in builtins.length ids == builtins.length (lib.unique ids);
      message = "envy.storage.rules must have unique IDs.";
    }
    {
      assertion = lib.all (rule: lib.hasPrefix "/" rule.path) config.envy.storage.rules;
      message = "envy.storage.rules paths must be absolute directories.";
    }
  ];
}
