{ lib, piSource, ... }:

let
  # Use the same locked source as the Pi executable, so extension APIs and
  # imports cannot drift independently. These need only Pi's bundled modules.
  extensionRoot = piSource + "/packages/coding-agent/examples/extensions";
  extensions = [ "questionnaire" "todo" "tools" "handoff" ];
in
{
  # Own individual, prefixed files instead of the extensions directory or
  # settings.json. Pi retains ownership of packages, sessions, and tool state.
  # OMP has a different extension API; these are upstream-Pi extensions only.
  home.file = lib.listToAttrs (map (name:
    lib.nameValuePair ".pi/agent/extensions/envy-${name}.ts" {
      source = extensionRoot + "/${name}.ts";
    }
  ) extensions);
}
