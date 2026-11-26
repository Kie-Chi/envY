{ config, lib, machinePlatform, ... }:

{
  # Feature composition only. Shared agent, endpoint, and skill policy lives in
  # defaults.nix; each provider owns its package, wrapper, and settings target.
  imports = [
    ./options.nix
    ./defaults.nix
    ./runtime-files.nix
    ./diagnostics.nix
    ./amux
    ./providers
    ./skills
    ./tools
  ] ++ lib.optionals (machinePlatform == "darwin") [ ./darwin ]
    ++ lib.optionals (machinePlatform == "linux") [ ./linux ];

  config.assertions = lib.mapAttrsToList (name: agent: {
    assertion = agent.models == [] || (name == "pi" && agent.adapter == "pi") || (name == "codex" && agent.adapter == "codex");
    message = "Model lists require envy.agents.pi or envy.agents.codex with the matching adapter.";
  }) config.envy.agents;
}
