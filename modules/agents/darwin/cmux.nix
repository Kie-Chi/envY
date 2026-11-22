{ config, lib, pkgs, osConfig, ... }:

let
  casks = osConfig.envy.darwin.software.homebrew.casks.effective;
  selected = builtins.elem "cmux" casks;
  hooksSelected = !(builtins.elem "envy-cmux-hooks" config.envy.software.nix.packages.exclude);
  piSelected = !(builtins.elem "pi" config.envy.software.nix.packages.exclude);
  codexSelected = builtins.elem "codex" casks;
  bridge = "${config.xdg.configHome}/envy/cmux/pi-session.mjs";
  setup = pkgs.writeShellApplication {
    name = "envy-cmux-hooks";
    runtimeInputs = [ pkgs.nodejs_24 ];
    text = ''
      exec node --disable-warning=ExperimentalWarning ${../../../files/agents/cmux/setup.mjs} \
        /opt/homebrew/bin/cmux ${lib.escapeShellArg bridge} \
        ${lib.escapeShellArgs (lib.optional piSelected "pi" ++ lib.optional codexSelected "codex")}
    '';
  };
in
{
  config = lib.mkIf selected {
    envy.software.nix.packages = {
      include = [ setup ];
      references.envy-cmux-hooks = "local:modules/agents/darwin/cmux.nix#setup";
    };
    home.file.".pi/agent/extensions/envy-cmux.ts" = lib.mkIf (hooksSelected && piSelected) {
      source = pkgs.replaceVars ../../../files/agents/pi/cmux.ts { cmuxPiSource = bridge; };
    };
    home.activation.cmuxAgentHooks = lib.mkIf hooksSelected (lib.hm.dag.entryAfter
      ([ "linkGeneration" ] ++ lib.optional codexSelected "mergeAgentRuntime-codex-settings") ''
      if [ -z "$DRY_RUN_CMD" ] && [ -x /opt/homebrew/bin/cmux ]; then
        ${lib.getExe setup}
      fi
    '');
  };
}
