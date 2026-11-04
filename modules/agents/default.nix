{ config, lib, machinePlatform, ... }:

{
  # Feature composition only. Shared agent, endpoint, and skill policy lives in
  # defaults.nix; each provider owns its package, wrapper, and settings target.
  imports = [
    ./options.nix
    ./defaults.nix
    ./runtime-files.nix
    ./amux
    ./providers
    ./skills
    ./tools
  ] ++ lib.optionals (machinePlatform == "darwin") [ ./darwin ]
    ++ lib.optionals (machinePlatform == "linux") [ ./linux ];

  config.assertions = lib.mapAttrsToList (name: agent: {
    assertion = agent.models == [] || (name == "pi" && agent.adapter == "pi");
    message = "Model lists are currently supported only by envy.agents.pi with the pi adapter.";
  }) config.envy.agents;
}
