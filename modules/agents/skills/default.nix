{ config, lib, ... }:

let
  skillsConfig = config.agents.skills;
  requestedSkills = lib.unique (skillsConfig.active
    ++ lib.concatLists (lib.mapAttrsToList (_: agent: agent.skills) config.envy.agents));
  # Adapter instances share a discovery directory. Resolve that directory's
  # selection once and use it for both Home Manager links and the manifest.
  canonicalTarget = target: if target == "claude-code" then "claude" else target;
  supports = target: skill:
    builtins.elem (canonicalTarget target) (map canonicalTarget skill.targets);
  selectedFor = target: lib.unique (lib.filter (name:
    builtins.hasAttr name skillsConfig.catalog && supports target skillsConfig.catalog.${name}
  ) (
    skillsConfig.active
    ++ lib.concatLists (lib.mapAttrsToList (_: agent:
      lib.optionals (canonicalTarget agent.adapter == canonicalTarget target) agent.skills
    ) config.envy.agents)
  ));
  filesFor = target: homePrefix:
    lib.mapAttrs'
      (name: skill:
        lib.nameValuePair "${homePrefix}/${name}" {
          source = skill.source;
        })
      (lib.filterAttrs (name: skill:
        builtins.elem name (selectedFor target) && supports target skill
      ) skillsConfig.catalog);

  unknownSkills = lib.subtractLists (builtins.attrNames skillsConfig.catalog) requestedSkills;
  incompatibleSkills = lib.concatLists (lib.mapAttrsToList (name: agent:
    map (skill: "${name}:${skill}") (lib.filter (skill:
      builtins.hasAttr skill skillsConfig.catalog
      && !(supports agent.adapter skillsConfig.catalog.${skill})
    ) agent.skills)
  ) config.envy.agents);
  missingManifests = lib.filter
    (name: !(builtins.pathExists (skillsConfig.catalog.${name}.source + "/SKILL.md")))
    (lib.intersectLists (builtins.attrNames skillsConfig.catalog) requestedSkills);
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

    effective = lib.mkOption {
      type = with lib.types; attrsOf (listOf str);
      readOnly = true;
      description = "Resolved discoverable catalog skills for each agent instance.";
    };
  };

  config = {
    agents.skills.effective = lib.mapAttrs (_: agent: selectedFor agent.adapter) config.envy.agents;
    assertions = [
      {
        assertion = unknownSkills == [ ];
        message = "Agent skill selections contain unknown skills: ${lib.concatStringsSep ", " unknownSkills}";
      }
      {
        assertion = incompatibleSkills == [ ];
        message = "Agent skill selections have incompatible targets: ${lib.concatStringsSep ", " incompatibleSkills}";
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
      (filesFor "stepgo" ".stepgo/skills")
      (filesFor "pi" ".pi/agent/skills")
    ];
  };
}
