{ config, lib, pkgs, sys, ... }:

let
  tools = config.envy.software.pypi.tools.effective;
  installTool = tool:
    let
      versionSuffix = if tool.version == null then "" else "==${tool.version}";
      packageSpec = "${tool.name}${versionSuffix}";
      withPackages = tool.parameters."with" or [ ];
      withFlags = lib.concatMapStringsSep " "
        (dependency: "--with ${lib.escapeShellArg dependency}")
        withPackages;
      indexFlags = lib.optionalString (tool.sourceId != null && tool.sourceId != "pypi:pypi")
        ("--index-url " + lib.escapeShellArg (tool.parameters.index or (throw "PyPI source requires index")));
      checkPath = "$HOME/.local/share/uv/tools/${tool.name}";
    in ''
      if ! [ -d "${checkPath}" ]; then
        ${pkgs.uv}/bin/uv tool install ${lib.escapeShellArg packageSpec} ${withFlags} ${indexFlags}
      fi
    '';
in
{
  config = lib.mkIf (tools != [ ]) {
    envy.machine.activationActions = [ {
      id = "uv-tools";
      owner = "modules/software/pypi.nix";
      manager = "uv";
      summary = "Ensure ${toString (builtins.length tools)} selected PyPI tools exist in the uv tool environment.";
      effects = [ "Install missing selected tools and declared companion dependencies into user uv tool environments; downloads and package builds may occur." ];
      verification = "Inspect selected tools and their uv environment directories; metadata inspection does not invoke uv or install packages.";
      repeatability = "Existing tool directories are skipped; activation does not reconcile their versions or remove deselected tools. This description does not authorize a retry.";
      rollbackBoundary = "A generation rollback does not uninstall uv tools or restore their environments, caches, and package build side effects.";
    } ];
    home.activation.installPypiTools = sys.task.activation {
      name = "pypi-tools";
      script = lib.concatMapStringsSep "\n" installTool tools;
    };
  };
}
