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
    home.activation.installPypiTools = sys.task.activation {
      name = "pypi-tools";
      script = lib.concatMapStringsSep "\n" installTool tools;
    };
  };
}
