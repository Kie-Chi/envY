# Exercise settings renderer aggregation with a fake adapter.
{ lib, pkgs }:

let
  evaluated = lib.evalModules {
    modules = [
      ../options.nix
      ../runtime-files.nix
      {
        config._module.args.sys = { mergeWritable = spec: spec; };
        options.home.activation = lib.mkOption {
          type = lib.types.attrsOf lib.types.anything;
          default = {};
        };
        options.assertions = lib.mkOption {
          type = lib.types.listOf lib.types.anything;
          default = [];
        };
        config.envy.agents = {
          codex = { adapter = "codex"; additionalSettings = { model = "machine-model"; }; };
          "codex-alt" = { adapter = "codex"; };
          cc = { adapter = "claude-code"; additionalSettings = { theme = "dark"; }; };
        };
        config.agents.settingsFiles = {
          codex = {
            target = ".codex/config.toml";
            format = "toml";
            aggregate = true;
            managedKeyPrefixes.model_providers = "envy";
            render = agent: {
              model = "rendered";
              model_providers."envy-${agent.adapter}-${lib.concatStringsSep "" (builtins.attrNames agent.additionalSettings)}" = {
                name = agent.adapter;
              };
            };
          };
          claude-code = { target = ".claude/settings.json"; format = "json"; };
        };
      }
    ];
  };
  config = evaluated.config;
  activation = config.home.activation;
  codex = activation.mergeAgentRuntime-codex-settings;
  claude = activation.mergeAgentRuntime-cc-settings;
in
assert lib.all (item: item.assertion) config.assertions;
assert builtins.attrNames activation == [
  "mergeAgentRuntime-cc-settings"
  "mergeAgentRuntime-codex-settings"
];
assert codex.target == ".codex/config.toml";
assert codex.format == "toml";
assert codex.managedKeyPrefixes == { model_providers = "envy"; };
assert builtins.attrNames codex.data.model_providers == [
  "envy-codex-"
  "envy-codex-model"
];
assert codex.data.model == "rendered";
assert claude.data == { theme = "dark"; };
pkgs.writeShellScript "agent-runtime-files-tests" ''
  echo "agent runtime-files checks passed"
''
