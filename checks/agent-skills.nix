{ inputs }:

let
  lib = inputs.nixpkgs.lib;
  source = inputs.emil-skills + "/skills/animate";
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
  hasFailure = config: text: builtins.any
    (item: !item.assertion && lib.hasInfix text item.message) config.assertions;
in
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
true
