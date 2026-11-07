{ config, lib, pkgs, piSource, piPackage, ... }:

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
    chmod -R u+w "$out"
    # Print, JSON, and RPC modes own stdout; only a terminal gets OSC alerts.
    # In-process subagent sessions have no UI and must not report readiness.
    substituteInPlace "$out/notify.ts" --replace-fail \
      'pi.on("agent_settled", async () => {' \
      'pi.on("agent_settled", async (_event, ctx) => { if (!process.stdout.isTTY || !ctx.hasUI) return;'
  '';
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
  piSelected = !(builtins.elem "pi" config.envy.software.nix.packages.exclude);
  pythonLspSelected = piSelected
    && !(builtins.elem "ruff" config.envy.software.nix.packages.exclude);
  # Reviewed npm packages pinned to registry integrity hashes. Check their Pi
  # peer range against the locked Pi before changing a version.
  views = import ./views.nix { inherit lib pkgs; };
  packages = {
    "context-view" = views.context;
    diff = views.diff;
    goal-x = import ./goal.nix { inherit lib pkgs piPackage; };
    queue-steer = import ./queue.nix { inherit lib pkgs piPackage; };
    lsp = import ./lsp.nix {
      inherit lib pkgs piPackage;
      pythonEnabled = pythonLspSelected;
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
    ++ [ (link ".pi/agent/extensions/envy-notify.ts" "${examples}/notify.ts") ]
  );

  # Each queued prompt gets its own delivery boundary rather than merging
  # several pending prompts into one turn. Machine settings may override this.
  envy.agents.pi.additionalSettings = {
    steeringMode = lib.mkDefault "one-at-a-time";
    followUpMode = lib.mkDefault "one-at-a-time";
  };

  # Merge only owned fields into writable files; Pi keeps session and queue
  # state. The queue editor derives next-row navigation from the dequeue key.
  envy.agents.pi.runtimeFiles = [
    {
      name = "queue-keybindings";
      target = ".pi/agent/keybindings.json";
      format = "json";
      data = {
        "tui.input.submit" = "enter";
        "app.message.followUp" = "alt+enter";
        "app.message.dequeue" = "alt+up";
      };
    }
    {
      name = "goal-settings";
      target = ".pi/agent/pi-goal-x-settings.json";
      format = "json";
      data = {
        hideUnfocusedBanner = true;
        hideUnfocusedPrompt = true;
        autoSelectSingleGoal = false;
      };
    }
  ];

  envy.software.nix.packages = {
    include = lib.optional pythonLspSelected packages.lsp.serverPackage;
    references = lib.optionalAttrs pythonLspSelected {
      ${lib.getName packages.lsp.serverPackage} = packages.lsp.serverReference;
    };
  };

  # pi-goal-x keeps per-project goal state beside the working tree.
  programs.git.ignores = [ ".pi/goals/" ".pi/.goals-pool-snapshot.json" ];
}
