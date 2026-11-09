{ inputs }:

let
  lib = inputs.nixpkgs.lib;
  source = inputs.emil-skills + "/skills/animate";
  sharedDefaults = import ../modules/agents/defaults.nix {
    inherit lib;
    config = { };
    academicResearchSkills = inputs.academic-research-skills;
    academicResearchSkillsCodex = inputs.academic-research-skills-codex;
    antiDefensiveWriting = inputs.anti-defensive-writing;
    emilSkills = inputs.emil-skills;
    gptImage2PptSkills = inputs.gpt-image2-ppt-skills;
    obeliskSkill = inputs.obelisk-skill;
    pptMaster = inputs.ppt-master;
    promptMaster = inputs.prompt-master;
    vercelAgentSkills = inputs.vercel-agent-skills;
    wazaSource = inputs.waza;
  };
  behaviorCases = builtins.fromJSON (builtins.readFile ../modules/agents/providers/pi/tests/skill-cases.json);
  maintenanceNames = [ "envy-maintain" "pi-maintain" ];
  maintenanceCatalog = lib.getAttrs maintenanceNames sharedDefaults.agents.skills.catalog;
  evaluate = policy: (lib.evalModules {
    modules = [
      ../modules/agents/options.nix
      ../modules/agents/skills/default.nix
      {
        options.assertions = lib.mkOption {
          type = lib.types.listOf lib.types.anything;
          default = [ ];
        };
        options.home.file = lib.mkOption {
          type = lib.types.attrsOf lib.types.anything;
          default = { };
        };
        config = {
          agents.skills = {
            catalog = {
              shared = { inherit source; targets = [ "pi" "codex" ]; };
              extra = { inherit source; targets = [ "pi" "codex" ]; };
              claude-only = { inherit source; targets = [ "claude-code" ]; };
              missing = { source = ../checks; targets = [ "pi" ]; };
            };
            active = [ "shared" "claude-only" ];
          };
          envy.agents = {
            pi = { adapter = "pi"; skills = [ "extra" "shared" ]; };
            other-pi.adapter = "pi";
            codex.adapter = "codex";
            cc.adapter = "claude-code";
          };
        };
      }
      policy
    ];
  }).config;
  valid = evaluate { };
  unknown = evaluate { envy.agents.pi.skills = lib.mkForce [ "unknown" ]; };
  incompatible = evaluate { envy.agents.pi.skills = lib.mkForce [ "claude-only" ]; };
  missing = evaluate { envy.agents.pi.skills = lib.mkForce [ "missing" ]; };
  maintenance = evaluate {
    agents.skills = {
      catalog = lib.mkForce maintenanceCatalog;
      active = lib.mkForce maintenanceNames;
    };
    envy.agents = {
      pi.skills = lib.mkForce [ ];
      claude.adapter = "claude";
    };
  };
  hasFailure = config: text: builtins.any
    (item: !item.assertion && lib.hasInfix text item.message) config.assertions;
in
assert behaviorCases.schemaVersion == 1;
assert builtins.all (case: builtins.all (name:
  builtins.hasAttr name sharedDefaults.agents.skills.catalog
  && builtins.elem "pi" sharedDefaults.agents.skills.catalog.${name}.targets
  && builtins.elem name sharedDefaults.agents.skills.active
) (case.requiredSkills ++ case.excludedSkills)) behaviorCases.cases;
assert builtins.all (item: item.assertion) valid.assertions;
assert valid.agents.skills.effective.pi == [ "shared" "extra" ];
assert valid.agents.skills.effective.other-pi == valid.agents.skills.effective.pi;
assert valid.agents.skills.effective.codex == [ "shared" ];
assert valid.agents.skills.effective.cc == [ "claude-only" ];
assert builtins.attrNames valid.home.file == [
  ".claude/skills/claude-only"
  ".codex/skills/shared"
  ".pi/agent/skills/extra"
  ".pi/agent/skills/shared"
];
assert hasFailure unknown "unknown skills";
assert hasFailure incompatible "incompatible targets";
assert hasFailure missing "missing SKILL.md";
assert builtins.all (name: builtins.elem name sharedDefaults.agents.skills.active) maintenanceNames;
assert builtins.all (item: item.assertion) maintenance.assertions;
assert builtins.all (skills: skills == maintenanceNames)
  (builtins.attrValues maintenance.agents.skills.effective);
assert builtins.attrNames maintenance.home.file == [
  ".claude/skills/envy-maintain"
  ".claude/skills/pi-maintain"
  ".codex/skills/envy-maintain"
  ".codex/skills/pi-maintain"
  ".pi/agent/skills/envy-maintain"
  ".pi/agent/skills/pi-maintain"
];
assert builtins.all (target: builtins.all (name:
  maintenance.home.file."${target}/${name}".source == maintenanceCatalog.${name}.source
) maintenanceNames) [ ".claude/skills" ".codex/skills" ".pi/agent/skills" ];
true
