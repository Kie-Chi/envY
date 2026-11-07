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
      # Pi exempts MCP tools from its allowlist unless it contains an mcp__
      # entry. This impossible tool name makes a child with no MCP capability
      # reject all MCP tools, including extension registrations after binding.
      substituteInPlace src/lifecycle/create-subagent-session.ts --replace-fail \
        'tools: [...toolNames, ...childTools.map((tool) => tool.name)],' \
        'tools: [...toolNames, "mcp__", ...childTools.map((tool) => tool.name)],'
    '';
  };
  # Explore and Plan retain their read-only roles without pinned models or bash.
  # Research and review roles inherit the caller's model and narrow MCP access.
  agents = [ "Explore" "Plan" "Researcher" "NixReviewer" "Reviewer" ];
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
