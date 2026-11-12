{ config, lib, machinePlatform, ... }:

let
  inherit (lib) mkOption types;
  absoluteDirectory = types.addCheck types.str (value: lib.hasPrefix "/" value);
  ruleOptions = import ./rule-options.nix { inherit lib; };
  ruleDefaults = lib.mapAttrs (_: option: option.default)
    (lib.filterAttrs (_: option: option ? default) ruleOptions);
  catalog = import ./rules {
    home = config.envy.user.home;
    inherit machinePlatform;
  };
  selection = config.envy.storage.rules;
  definitions = builtins.attrValues selection.include;
  ids = builtins.attrNames selection.include;
  selected = lib.filter (id: !(builtins.elem id selection.exclude)) ids;
  effectiveRule = id:
    let
      rule = selection.include.${id};
      defaults = ruleDefaults // (catalog.rules.${id} or { });
      overriddenFields = if catalog.rules ? ${id} then
        builtins.attrNames (lib.filterAttrs (field: value: value != defaults.${field}) rule)
        else [ ];
    in rule // {
      inherit id overriddenFields;
      source = catalog.sources.${id} or "machine";
    };
  archive = config.envy.storage.archive;
  mountedRepository = lib.hasPrefix "/" archive.repository;
  safeMountSource = value:
    builtins.match "[^[:cntrl:]]*" value != null
    && builtins.match "([A-Za-z][A-Za-z0-9+.-]*:)?//[^/@]*:[^/@]*@.*" value == null;
  applicationDataRoot = config.envy.user.home + "/Library/Application Support";
in
{
  options.envy.storage = {
    mode = mkOption {
      type = types.enum [ "manual" "report" "auto" ];
      default = "manual";
      description = "Storage execution mode; explicit run --yes remains available in report mode.";
    };
    quarantineDays = mkOption {
      type = types.ints.positive;
      default = 30;
      description = "Minimum quarantine retention in days.";
    };
    protectedPaths = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Additional paths excluded from storage lifecycle actions.";
    };
    protectedOverrides = mkOption {
      type = types.listOf absoluteDirectory;
      default = [ ];
      description = "Explicit machine authorizations; shared rules carry their own precise authorizations.";
    };
    rules = {
      include = mkOption {
        type = types.attrsOf (types.submodule { options = ruleOptions; });
        default = { };
        description = ''
          Shared and machine rules keyed by stable ID. Machines may override
          individual fields or add a definition. Legacy lists must be migrated
          to rules.include.<id>; IDs are taken from the attribute names.
        '';
      };
      exclude = mkOption {
        type = types.listOf types.str;
        default = [ ];
        apply = values: lib.sort builtins.lessThan (lib.unique values);
        description = "Rule IDs excluded from all contributions; exclusions win.";
      };
      effective = mkOption {
        type = types.listOf (types.attrsOf types.anything);
        readOnly = true;
        description = "Resolved rules with IDs, origins and overridden field names.";
      };
    };
    maintenance = lib.genAttrs [ "nix" "docker" "go" "uv" "npm" "pip" "brew" ]
      (name: mkOption {
        type = types.bool;
        default = false;
        description = "Allow explicit native ${name} cache maintenance; never scheduled.";
      });
    archive.mountSource = mkOption {
      type = types.str;
      default = "";
      description = ''
        Expected OS mount source identifier for an absolute repository, such as
        //fixture@nas/share. Must not contain passwords; leave empty for SFTP.
      '';
    };
    archive.repository = mkOption {
      type = types.addCheck types.str (value:
        value == "" || lib.hasPrefix "sftp:" value || lib.hasPrefix "/" value);
      default = "";
      description = "Dedicated restic SFTP repository or absolute mounted repository path.";
    };
  };

  config = {
    envy.storage.rules = {
      include = lib.mapAttrs (_: rule: lib.mapAttrs (_: value: lib.mkDefault value) rule) catalog.rules;
      # Native Go maintenance clears the whole build cache; never also schedule
      # age-based quarantine of the same cache.
      exclude = lib.optional config.envy.storage.maintenance.go "go-build-cache";
      effective = map effectiveRule selected;
    };
    assertions = [
      {
        assertion = !mountedRepository || archive.mountSource != "";
        message = "Absolute storage archive repositories require archive.mountSource.";
      }
      {
        assertion = archive.mountSource == "" || mountedRepository;
        message = "Storage archive.mountSource requires an absolute repository path.";
      }
      {
        assertion = safeMountSource archive.mountSource;
        message = "Storage archive.mountSource must not contain control characters or authority passwords.";
      }
      {
        assertion = lib.all (id: builtins.match "[a-z][a-z0-9-]*" id != null) ids;
        message = "envy.storage.rules.include keys must be stable lowercase rule IDs.";
      }
      {
        assertion = lib.all (id: builtins.hasAttr id selection.include) selection.exclude;
        message = "envy.storage.rules.exclude contains an unknown rule ID.";
      }
      {
        assertion = lib.all (rule: lib.hasPrefix "/" rule.path) definitions;
        message = "envy.storage rules must use absolute paths.";
      }
      {
        assertion = lib.all (rule: rule.targets != "dirs" || rule.extensions == [ ]) definitions;
        message = "Directory storage rules cannot filter by extension.";
      }
      {
        assertion = lib.all (rule: !rule.recursiveAge || rule.targets == "dirs") definitions;
        message = "Storage recursiveAge requires directory targets.";
      }
      {
        assertion = lib.all (rule: lib.all (name: name != "" && !(lib.hasInfix "/" name)) rule.names) definitions;
        message = "Storage rule names must be plain entry names.";
      }
      {
        assertion = lib.all (rule: lib.all (process: process != "") rule.processes) definitions;
        message = "Storage process patterns must not be empty.";
      }
      {
        assertion = lib.all (path: path != applicationDataRoot
          && !(builtins.elem path config.envy.storage.protectedPaths))
          (config.envy.storage.protectedOverrides
            ++ lib.concatMap (rule: rule.protectedOverrides) selection.effective);
        message = "Storage protected overrides must name specific application cache directories.";
      }
    ];
  };
}
