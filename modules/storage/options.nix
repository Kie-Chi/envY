{ config, lib, ... }:

let
  inherit (lib) mkOption types;
  absoluteDirectory = types.addCheck types.str (value: lib.hasPrefix "/" value);
  ruleIds = map (rule: rule.id) config.envy.storage.rules;
  applicationDataRoot = config.envy.user.home + "/Library/Application Support";
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
    protectedOverrides = mkOption {
      type = types.listOf absoluteDirectory;
      default = [ ];
      description = ''
        Application data directories that become eligible despite their
        protected parent. Entries must name a specific cache or update
        directory; a protected parent can never be overridden as a whole.
      '';
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
            description = ''
              Absolute directory scanned by this rule. Shell-style wildcards
              are expanded by envY before organizing; organize itself does not
              expand them.
            '';
          };
          action = mkOption {
            type = types.enum [ "quarantine" "archive" ];
            description = "Action for matching entries.";
          };
          targets = mkOption {
            type = types.enum [ "files" "dirs" ];
            default = "files";
            description = "Whether the rule selects regular files or directories.";
          };
          depth = mkOption {
            type = types.nullOr types.ints.positive;
            default = null;
            description = ''
              Maximum scan depth below each root, where 1 selects direct
              children. Null scans the whole subtree.
            '';
          };
          names = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Exact entry names to match; empty matches every name.";
          };
          recursiveAge = mkOption {
            type = types.bool;
            default = false;
            description = ''
              Require the entry and every descendant to be older than
              minAgeDays. Only applies to directory rules.
            '';
          };
          processes = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = ''
              Process names that must not run while the rule is planned or
              executed. A pattern containing a slash is matched against the
              full command line.
            '';
          };
          extensions = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "File extensions to match; empty matches all extensions.";
          };
          minAgeDays = mkOption {
            type = types.ints.positive;
            default = 30;
            description = "Minimum entry age in days.";
          };
          minSizeBytes = mkOption {
            type = types.ints.unsigned;
            default = 0;
            description = "Minimum entry size in bytes.";
          };
        };
      });
      default = [ ];
      description = "Explicit storage lifecycle rules.";
    };
    maintenance = {
      nix = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Allow explicitly requested Nix garbage collection that keeps all
          generations. Never runs from the storage schedule.
        '';
      };
      docker = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Allow explicitly requested Docker build-cache and dangling-image
          pruning. Containers, volumes, and tagged images are never touched.
        '';
      };
      go = mkOption {
        type = types.bool;
        default = false;
        description = "Allow explicitly requested Go build-cache cleanup.";
      };
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
      assertion = builtins.length ruleIds == builtins.length (lib.unique ruleIds);
      message = "envy.storage.rules must have unique IDs.";
    }
    {
      assertion = lib.all (rule: lib.hasPrefix "/" rule.path) config.envy.storage.rules;
      message = "envy.storage.rules must use absolute paths.";
    }
    {
      assertion = lib.all (rule: rule.targets != "dirs" || rule.extensions == [])
        config.envy.storage.rules;
      message = "envy.storage.rules with targets = dirs cannot filter by extension.";
    }
    {
      assertion = lib.all (rule: !rule.recursiveAge || rule.targets == "dirs")
        config.envy.storage.rules;
      message = "envy.storage.rules recursiveAge requires targets = dirs.";
    }
    {
      assertion = lib.all (rule: lib.all (name: name != "" && !(lib.hasInfix "/" name)) rule.names)
        config.envy.storage.rules;
      message = "envy.storage.rules names must be plain entry names.";
    }
    {
      assertion = lib.all (rule: lib.all (process: process != "") rule.processes)
        config.envy.storage.rules;
      message = "envy.storage.rules process patterns must not be empty.";
    }
    {
      assertion = lib.all (path: path != applicationDataRoot
        && !(builtins.elem path config.envy.storage.protectedPaths))
        config.envy.storage.protectedOverrides;
      message = "envy.storage.protectedOverrides must name a specific protected directory.";
    }
  ];
}
