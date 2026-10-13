{ config, lib, sys, ... }:

let
  settingsFileFor = agent:
    if agent.additionalSettingsFile != null then agent.additionalSettingsFile
    else if builtins.hasAttr agent.adapter config.agents.settingsFiles
      then config.agents.settingsFiles.${agent.adapter}
      else null;
  missingTargets = lib.attrNames (lib.filterAttrs
    (_: agent: agent.additionalSettings != {} && settingsFileFor agent == null)
    config.envy.agents);
  runtimeFiles = lib.concatLists (lib.mapAttrsToList (agentName: agent:
    let settingsFile = settingsFileFor agent;
    in map (file: file // { name = "${agentName}-${file.name}"; }) (
      (lib.optional (agent.additionalSettings != {} && settingsFile != null) {
        name = "settings";
        target = settingsFile.target;
        format = settingsFile.format;
        data = agent.additionalSettings;
      }) ++ agent.runtimeFiles
    )
  ) config.envy.agents);
in
{
  options.agents.settingsFiles = lib.mkOption {
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        target = lib.mkOption {
          type = lib.types.nonEmptyStr;
          description = "Home-relative application configuration path.";
        };
        format = lib.mkOption {
          type = lib.types.enum [ "json" "yaml" "toml" ];
          default = "json";
          description = "Application configuration format.";
        };
      };
    });
    default = {};
    description = "Default settings file metadata supplied by each agent adapter.";
  };

  config = {
    home.activation = lib.listToAttrs (map (file:
      lib.nameValuePair "mergeAgentRuntime-${file.name}" (sys.mergeWritable {
        name = "mergeAgentRuntime-${file.name}";
        inherit (file) target format data;
      })
    ) runtimeFiles);
    assertions = [
      {
        assertion = missingTargets == [];
        message = "Agent additionalSettings requires settings file metadata: ${lib.concatStringsSep ", " missingTargets}";
      }
      {
        assertion = builtins.length runtimeFiles == builtins.length (lib.unique (map (file: file.name) runtimeFiles));
        message = "Agent runtime configuration activation names must be unique.";
      }
      {
        assertion = lib.all (file:
          !(lib.hasPrefix "/" file.target) && !(builtins.elem ".." (lib.splitString "/" file.target))
        ) runtimeFiles;
        message = "Agent runtime configuration targets must be home-relative paths without parent traversal.";
      }
    ];
  };
}
