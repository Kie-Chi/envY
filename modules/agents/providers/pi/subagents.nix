{ lib, pkgs, ... }:

let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
  # In-process subagents with background runs, resume, and steering. Its peer
  # range is Pi >= 1.0 and it has no runtime dependencies.
  subagents = fetchPiExtension {
    pname = "pi-subagents";
    version = "25.0.5";
    url = "https://registry.npmjs.org/@gotgenes/pi-subagents/-/pi-subagents-25.0.5.tgz";
    hash = "sha512-qS9vNlmdIBuC/RjS8HH5unI/624hXmSqdKKsqI1xiOebqow4JHM+AHev3KeUbXOG+cKdSU1oOffPwPdu7MxfJw==";
    # A qualified "provider/model" names one model: fail instead of fuzzy
    # matching a different available model when it is unavailable.
    postUnpack = ''
      substituteInPlace src/session/model-resolver.ts --replace-fail \
        'const bestMatch = findBestFuzzyMatch(all, input.toLowerCase());' \
        'const bestMatch = slashIdx === -1 ? findBestFuzzyMatch(all, input.toLowerCase()) : undefined;'
    '';
  };
  # Built-in Explore pins an Anthropic model and both read-only defaults
  # expect bash; these overrides inherit the caller's model and use codemode.
  agents = [ "Explore" "Plan" ];
  link = target: source: lib.nameValuePair target { inherit source; };
in
{
  # subagents.json, child sessions, and their transcripts stay Pi-owned.
  home.file = lib.listToAttrs (
    [ (link ".pi/agent/extensions/envy-subagents" subagents) ]
    # The file name is the agent type, so these replace the built-ins.
    ++ map (name: link ".pi/agent/agents/${name}.md" ../../../../files/agents/subagents/${name}.md) agents
  );
}
