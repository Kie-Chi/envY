{ config, lib, pkgs, piSource, ... }:

let
  # Use the same locked source as the Pi executable, so extension APIs and
  # imports cannot drift independently. These need only Pi's bundled modules.
  extensionRoot = piSource + "/packages/coding-agent/examples/extensions";
  extensions = [ "questionnaire" "todo" "tools" "handoff" ];
  # Upstream examples assume Pi runs interactively from a checkout. Adapt only
  # those assumptions; --replace-fail turns upstream drift into a build error.
  examples = pkgs.runCommand "pi-envy-examples" { } ''
    mkdir -p "$out"
    cp ${extensionRoot}/notify.ts "$out/notify.ts"
    cp -r ${extensionRoot}/subagent "$out/subagent"
    chmod -R u+w "$out"
    # Print, JSON, and RPC modes own stdout; only a terminal gets OSC alerts.
    substituteInPlace "$out/notify.ts" --replace-fail \
      'pi.on("agent_settled", async () => {' \
      'pi.on("agent_settled", async () => { if (!process.stdout.isTTY) return;'
    # Children use the envY wrapper for its system prompt and credentials.
    substituteInPlace "$out/subagent/index.ts" --replace-fail \
      'function getPiInvocation(args: string[]): { command: string; args: string[] } {' \
      'function getPiInvocation(args: string[]): { command: string; args: string[] } { return { command: ${builtins.toJSON "${config.home.profileDirectory}/bin/pi"}, args };'
    # Without a pinned model, children inherit the caller's model.
    sed -i '/^model:/d' "$out"/subagent/agents/*.md
  '';
  subagentAgents = [ "scout" "planner" "reviewer" "worker" ];
  subagentPrompts = [ "implement" "scout-and-plan" "implement-and-review" ];
  fetchPiExtension = import ./pi-npm-extension.nix { inherit pkgs; };
  # Reviewed npm packages pinned to registry integrity hashes. Check their Pi
  # peer range against the locked Pi before changing a version.
  packages = {
    # 0.34 and later require Pi 1.4.
    goal-x = fetchPiExtension {
      pname = "pi-goal-x";
      version = "0.33.0";
      url = "https://registry.npmjs.org/pi-goal-x/-/pi-goal-x-0.33.0.tgz";
      hash = "sha512-FJdd4Fz9gWNzTybovN/BJp83o5BdgOytHZIeddBxU21HNFciEpFV83yVLpJO2lFIEvOLbXLeNJtMhk138urPGA==";
    };
    btw = fetchPiExtension {
      pname = "rpiv-btw";
      version = "2.15.0";
      url = "https://registry.npmjs.org/@juicesharp/rpiv-btw/-/rpiv-btw-2.15.0.tgz";
      hash = "sha512-lJ1uelu5id+aQctYdpi6Plk7JQu5QkwNDqkTPgzmnxZAktgyI8JlNvDnRiGOUUnLHjLxvqHZzPmugYyJmUG/4Q==";
    };
  };
  link = target: source: lib.nameValuePair target { inherit source; };
in
{
  # Own individual, prefixed entries instead of the extensions directory or
  # settings.json. Pi retains ownership of packages, sessions, and tool state.
  home.file = lib.listToAttrs (
    map (name: link ".pi/agent/extensions/envy-${name}.ts" (extensionRoot + "/${name}.ts")) extensions
    ++ lib.mapAttrsToList (name: link ".pi/agent/extensions/envy-${name}") packages
    ++ [
      (link ".pi/agent/extensions/envy-notify.ts" "${examples}/notify.ts")
      (link ".pi/agent/extensions/envy-subagent" "${examples}/subagent")
    ]
    # Agent names come from frontmatter; prompt file names become commands.
    ++ map (name: link ".pi/agent/agents/envy-${name}.md" "${examples}/subagent/agents/${name}.md") subagentAgents
    ++ map (name: link ".pi/agent/prompts/${name}.md" "${examples}/subagent/prompts/${name}.md") subagentPrompts
  );

  # pi-goal-x keeps per-project goal state beside the working tree.
  programs.git.ignores = [ ".pi/goals/" ".pi/.goals-pool-snapshot.json" ];
}
