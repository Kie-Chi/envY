{ config, lib, sys, ... }:

let
  settingsFileFor = agent:
    if agent.additionalSettingsFile != null then agent.additionalSettingsFile
    else if builtins.hasAttr agent.adapter config.agents.settingsFiles
      then config.agents.settingsFiles.${agent.adapter}
      else null;

  renderFor = settingsFile: agent:
    let render = settingsFile.render or null;
    in if render == null then {} else render agent;

  dataFor = agent: renderFor (settingsFileFor agent) agent // agent.additionalSettings;

  settingsEntry = { name, settingsFile, data }: {
    inherit name data;
    inherit (settingsFile) target format;
    managedKeyPrefixes = settingsFile.managedKeyPrefixes or {};
  };

  perAgentEntries = lib.concatLists (lib.mapAttrsToList (agentName: agent:
    let settingsFile = settingsFileFor agent; in
    if settingsFile == null || (settingsFile.aggregate or false) then [] else
    let data = dataFor agent; in
    map (file: file // { name = "${agentName}-${file.name}"; }) (
      (lib.optional (data != {}) (settingsEntry {
        name = "settings";
        inherit settingsFile data;
      })) ++ agent.runtimeFiles
    )
  ) config.envy.agents);

  # Aggregate adapters own one file for every instance, so a per-instance
  # write cannot erase the provider definitions contributed by its siblings.
  aggregatedEntries = lib.concatLists (lib.mapAttrsToList (adapter: agents:
    let settingsFile = settingsFileFor (builtins.head agents); in
    if settingsFile == null || !(settingsFile.aggregate or false) then [] else
    let data = lib.foldl' (acc: agent: lib.recursiveUpdate acc (dataFor agent)) {} agents; in
    lib.optional (data != {}) (settingsEntry {
      name = "${adapter}-settings";
      inherit settingsFile data;
    })
  ) (lib.groupBy (agent: agent.adapter) (lib.attrValues config.envy.agents)));

  runtimeFiles = perAgentEntries ++ aggregatedEntries;

  missingTargets = lib.attrNames (lib.filterAttrs
    (_: agent: dataFor agent != {} && settingsFileFor agent == null)
    config.envy.agents);
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
        render = lib.mkOption {
          type = lib.types.nullOr (lib.types.functionTo (lib.types.attrsOf lib.types.anything));
          default = null;
          description = "Adapter renderer for one agent; runs before machine additionalSettings.";
        };
        aggregate = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Merge every instance of this adapter into one settings file.";
        };
        managedKeyPrefixes = lib.mkOption {
          type = lib.types.attrsOf lib.types.str;
          default = {};
          description = "Object sections whose adapter-owned key namespaces are replaced.";
        };
      };
    });
    default = {};
    description = "Default settings file metadata supplied by each agent adapter.";
  };

  config = {
    home.activation = lib.listToAttrs (map (file:
      lib.nameValuePair "mergeAgentRuntime-${file.name}" (sys.mergeWritable {
        inherit (file) name target format data managedKeyPrefixes;
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
