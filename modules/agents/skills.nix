{ config, lib, ... }:

let
  skillsConfig = config.agents.skills;
  enabledCatalog = lib.filterAttrs
    (name: _: builtins.elem name skillsConfig.active)
    skillsConfig.catalog;

  filesFor = target: homePrefix:
    lib.mapAttrs'
      (name: skill:
        lib.nameValuePair "${homePrefix}/${name}" {
          source = skill.source;
        })
      (lib.filterAttrs (_: skill: builtins.elem target skill.targets) enabledCatalog);

  unknownSkills = lib.subtractLists (builtins.attrNames skillsConfig.catalog) skillsConfig.active;
  missingManifests = lib.filter
    (name: !(builtins.pathExists (skillsConfig.catalog.${name}.source + "/SKILL.md")))
    (builtins.attrNames enabledCatalog);
in
{
  options.agents.skills = {
    catalog = lib.mkOption {
      default = { };
      description = "Available skill packages, keyed by their skill name.";
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          source = lib.mkOption {
            type = lib.types.path;
            description = "Directory containing SKILL.md and optional skill resources.";
          };

          targets = lib.mkOption {
            type = with lib.types; listOf str;
            default = [ "codex" "claude-code" ];
            description = "Agent adapter IDs that should discover this skill when it is active.";
          };
        };
      });
    };

    active = lib.mkOption {
      type = with lib.types; listOf str;
      default = [ ];
      description = "Catalog skill names made discoverable on this machine.";
    };
  };

  config = {
    assertions = [
      {
        assertion = unknownSkills == [ ];
        message = "agents.skills.active contains unknown skills: ${lib.concatStringsSep ", " unknownSkills}";
      }
      {
        assertion = missingManifests == [ ];
        message = "Agent skill packages are missing SKILL.md: ${lib.concatStringsSep ", " missingManifests}";
      }
    ];

    # Only the selected skill directories are linked. The agents initially see
    # name/description metadata and load the SKILL.md body after a trigger.
    home.file = lib.mkMerge [
      (filesFor "codex" ".codex/skills")
      (filesFor "claude" ".claude/skills")
      (filesFor "claude-code" ".claude/skills")
      (filesFor "stepgo" ".stepgo/skills")
      (filesFor "pi" ".pi/agent/skills")
    ];
  };
}
