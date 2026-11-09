{ config, lib, ... }:

let
  canonical = adapter: if adapter == "claude-code" then "claude" else adapter;
  prefixes = { claude = ".claude/skills"; codex = ".codex/skills"; };
  targets = lib.unique (lib.concatLists (lib.mapAttrsToList (name: agent:
    let adapter = canonical agent.adapter;
    in lib.optionals (builtins.hasAttr adapter prefixes) (map (skill:
      "${prefixes.${adapter}}/${skill}"
    ) config.agents.skills.effective.${name})
  ) config.envy.agents));
  skills = map (target:
    let file = config.home.file.${target};
    in {
      name = builtins.baseNameOf target;
      adapter = if lib.hasPrefix ".claude/" target then "claude" else "codex";
      inherit target;
      source = toString file.source;
      recursive = file.recursive or false;
    }
  ) targets;
in
{
  # Describe only selected catalog skills, never application-owned configuration.
  home.file.".config/envy/agent-skills.json".text = builtins.toJSON {
    schemaVersion = 1;
    inherit skills;
  };
}
