{ config, lib, pkgs, sys, ... }:

let
  tools = config.envy.software.npm.tools.effective;
  npmPrefix = "$HOME/.npm-global";
  installTool = tool:
    let
      packageSpec = if tool.version == null then tool.name else "${tool.name}@${tool.version}";
      registryFlags = lib.optionalString (tool.sourceId != null && tool.sourceId != "npm:npmjs")
        ("--registry " + lib.escapeShellArg (tool.parameters.registry or (throw "npm source requires registry")));
      checkPath = "$HOME/.npm-global/lib/node_modules/${tool.name}";
    in ''
      if ! [ -d "${checkPath}" ]; then
        ${pkgs.nodejs_26}/bin/npm install -g ${lib.escapeShellArg packageSpec} ${registryFlags}
      fi
    '';
in
{
  config = lib.mkIf (tools != [ ]) {
    envy.machine.activationActions = [ {
      id = "npm-tools";
      owner = "modules/software/npm.nix";
      manager = "npm";
      summary = "Ensure ${toString (builtins.length tools)} selected NPM tools exist in the user prefix.";
      effects = [ "Install missing selected tools under ~/.npm-global and populate ~/.cache/npm; package lifecycle scripts may run." ];
      verification = "Inspect selected tools and their installation directories; metadata inspection does not invoke npm or install packages.";
      repeatability = "Existing package directories are skipped; activation does not reconcile their versions or remove deselected tools. This description does not authorize a retry.";
      rollbackBoundary = "A generation rollback does not uninstall global NPM tools, clear their cache, or reverse package lifecycle scripts.";
    } ];
    home.sessionVariables = {
      npm_config_prefix = npmPrefix;
      npm_config_cache = "$HOME/.cache/npm";
    };
    home.sessionPath = [ "${npmPrefix}/bin" ];

    home.activation.installNpmTools = sys.task.activation {
      name = "npm-tools";
      script = ''
        mkdir -p ${npmPrefix}/bin ${npmPrefix}/lib/node_modules "$HOME/.cache/npm"
        export npm_config_prefix=${npmPrefix}
        export npm_config_cache="$HOME/.cache/npm"
        ${lib.concatMapStringsSep "\n" installTool tools}
      '';
    };
  };
}
