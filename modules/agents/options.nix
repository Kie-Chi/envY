{ lib, ... }:

let
  inherit (lib) mkOption types;
in
{
  options.envy.agents = mkOption {
    type = types.attrsOf (types.submodule ({ ... }: {
      options = {
        adapter = mkOption {
          type = types.nonEmptyStr;
          description = "Agent adapter ID that owns package, command, env mapping, and skills integration.";
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
    description = "Machine-owned agent instances and their endpoint/model selection.";
  };
}
