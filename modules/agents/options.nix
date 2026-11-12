{ config, lib, ... }:

let
  inherit (lib) mkOption types;
  explicitThinkingLevelMap = options: mkOption {
    type = types.bool;
    internal = true;
    readOnly = true;
    default = options.thinkingLevelMap.highestPrio < 1500;
  };
  stripThinkingDefaults = value: builtins.removeAttrs value ([ "_explicitThinkingLevelMap" ]
    ++ lib.optional (!value._explicitThinkingLevelMap) "thinkingLevelMap");
  thinkingLevelMapOption = mkOption {
    type = types.nullOr (types.attrsOf (types.nullOr types.nonEmptyStr));
    default = null;
    apply = value: if value == null || lib.all (key: builtins.elem key
      [ "off" "minimal" "low" "medium" "high" "xhigh" "max" "ultra" ]) (builtins.attrNames value)
      then value else throw "thinkingLevelMap: unknown Pi thinking level.";
    description = "Pi-only thinking level overrides: null entries disable levels; null clears an inherited map and restores compatibility defaults.";
  };
in
{
  options.envy.agentProfiles = mkOption {
    type = types.attrsOf (types.submodule ({ options, ... }: {
      options = {
        _explicitThinkingLevelMap = explicitThinkingLevelMap options;
        endpoint = mkOption { type = types.nonEmptyStr; description = "Named endpoint for this reusable agent profile."; };
        credential = mkOption { type = types.nullOr types.str; default = null; description = "Logical credential reference for this reusable agent profile."; };
        api = mkOption { type = types.nullOr (types.enum [ "openai-completions" "openai-responses" "anthropic-messages" ]); default = null; description = "Optional wire API override."; };
        model = mkOption { type = types.nonEmptyStr; description = "Model selected by this reusable agent profile."; };
        contextWindow = mkOption { type = types.nullOr types.ints.positive; default = null; };
        maxTokens = mkOption { type = types.nullOr types.ints.positive; default = null; };
        thinkingLevelMap = thinkingLevelMapOption;
      };
    }));
    default = {};
    apply = lib.mapAttrs (_: stripThinkingDefaults);
    description = "Reusable non-sensitive agent model, endpoint, and credential selections.";
  };

  options.envy.agents = mkOption {
    type = types.attrsOf (types.submodule ({ options, ... }: {
      options = {
        _explicitThinkingLevelMap = explicitThinkingLevelMap options;
        thinkingLevelMap = thinkingLevelMapOption;
        adapter = mkOption {
          type = types.nonEmptyStr;
          description = "Agent adapter ID that owns package, command, env mapping, and skills integration.";
        };
        profile = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Reusable envy.agentProfiles entry used to populate runtime selections.";
        };
        endpoint = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Named envy.llm endpoint used by this agent.";
        };
        credential = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Logical encrypted credential reference used by this agent.";
        };
        api = mkOption {
          type = types.nullOr (types.enum [ "openai-completions" "openai-responses" "anthropic-messages" ]);
          default = null;
          description = "Optional wire API override; Pi derives it from the endpoint protocol when unset.";
        };
        model = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Default model passed to this agent, when supported.";
        };
        models = mkOption {
          type = types.listOf (types.submodule ({ options, ... }: {
            options = {
              profile = mkOption { type = types.nullOr types.nonEmptyStr; default = null; };
              endpoint = mkOption { type = types.nullOr types.nonEmptyStr; default = null; };
              credential = mkOption { type = types.nullOr types.nonEmptyStr; default = null; };
              api = mkOption {
                type = types.nullOr (types.enum [ "openai-completions" "openai-responses" "anthropic-messages" ]);
                default = null;
              };
              model = mkOption { type = types.nullOr types.nonEmptyStr; default = null; };
              contextWindow = mkOption { type = types.nullOr types.ints.positive; default = null; };
              maxTokens = mkOption { type = types.nullOr types.ints.positive; default = null; };
              thinkingLevelMap = thinkingLevelMapOption;
              _explicitFields = mkOption {
                type = types.listOf types.str;
                internal = true;
                readOnly = true;
                default = lib.filter (field: options.${field}.highestPrio < 1500)
                  [ "profile" "endpoint" "credential" "api" "model" "contextWindow" "maxTokens" "thinkingLevelMap" ];
              };
            };
          }));
          # Strip only schema defaults: an explicitly assigned null must still
          # clear a profile value. Keep this bookkeeping out of the manifest.
          apply = map (item: lib.getAttrs item._explicitFields item);
          default = [];
          description = "Pi or Codex model candidates with profile inheritance and explicit overrides. The legacy selection remains the default; otherwise the first entry is used. Codex requires one endpoint, credential, and output limit with the Responses API.";
        };
        contextWindow = mkOption {
          type = types.nullOr types.ints.positive;
          default = null;
          description = "Optional model context window, in tokens, for adapters that expose model metadata.";
        };
        maxTokens = mkOption {
          type = types.nullOr types.ints.positive;
          default = null;
          description = "Optional model output token limit, in tokens, for adapters that expose model metadata.";
        };
        moreThinking = mkOption {
          type = types.bool;
          default = false;
          description = "For Pi, enable the ultra-thinking patch and expose xhigh, max, and ultra for a model endpoint that supports these efforts.";
        };
        command = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Optional executable override for generic adapters.";
        };
        args = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Arguments injected before caller-provided agent arguments.";
        };
        aliases = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Compatibility command names for this agent.";
        };
        skills = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Additional skill catalog entries beyond agents.skills.active. Instances of an adapter share their discovery directory.";
        };
        additionalSettings = mkOption {
          # attrsOf anything, not attrs: the latter merges shallowly and leaves
          # nested mkDefault wrappers unevaluated, serializing a literal
          # {_type="override";...} object into the agent's settings file.
          type = types.attrsOf types.anything;
          default = { };
          description = "Provider-specific settings merged into the agent's writable application file.";
        };
        additionalSettingsFile = mkOption {
          type = types.nullOr (types.submodule ({ ... }: {
            options = {
              target = mkOption {
                type = types.nonEmptyStr;
                description = "Home-relative application configuration path for additionalSettings.";
              };
              format = mkOption {
                type = types.enum [ "json" "yaml" "toml" ];
                default = "json";
                description = "Structured format of the application configuration file.";
              };
            };
          }));
          default = null;
          description = "Application-owned file receiving additionalSettings during activation.";
        };
        runtimeFiles = mkOption {
          type = types.listOf (types.submodule ({ ... }: {
            options = {
              name = mkOption {
                type = types.nonEmptyStr;
                description = "Stable activation name for an application-owned runtime file.";
              };
              target = mkOption {
                type = types.nonEmptyStr;
                description = "Home-relative application runtime file path.";
              };
              format = mkOption {
                type = types.enum [ "json" "yaml" "toml" ];
                default = "json";
                description = "Structured format of the application runtime file.";
              };
              managedKeyPrefixes = mkOption {
                type = types.attrsOf types.str;
                default = {};
                description = "Object sections whose adapter-owned key namespaces are replaced, preserving all other keys.";
              };
              data = mkOption {
                type = types.attrs;
                description = "Declarative object merged into the runtime file.";
              };
            };
          }));
          default = [ ];
          description = "Application-owned runtime files declared by an agent adapter.";
        };
      };
    }));
    default = { };
    apply = lib.mapAttrs (name: agent: let
      profileMap = value: (config.envy.agentProfiles.${if (value.profile or null) == null then "" else value.profile} or {}).thinkingLevelMap or null;
      hasMap = value: (value.thinkingLevelMap or null) != null || profileMap value != null;
      cleaned = stripThinkingDefaults agent;
    in if agent.adapter != "pi" && (hasMap agent || lib.any hasMap agent.models)
      then throw "envy.agents.${name}.thinkingLevelMap: thinkingLevelMap requires the pi adapter."
      else cleaned);
    description = "Machine-owned agent instances and their endpoint/model selection.";
  };

  # Kept outside envy.agents, whose every key is an agent instance.
  options.envy.amux.skipPermissions = mkOption {
    type = types.bool;
    default = true;
    description = ''
      Start amux container agents with their permission prompts disabled.
      `amux new --no-skip-permissions` keeps the prompts for one session.
    '';
  };
}
