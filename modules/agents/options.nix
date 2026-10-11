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
          type = types.enum [ "openai-completions" "openai-responses" ];
          default = "openai-completions";
          description = "OpenAI wire API used by compatible agent adapters.";
        };
        model = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Default model passed to this agent, when supported.";
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
          description = "Skill catalog entries made available to this agent.";
        };
      };
    }));
    default = { };
    description = "Machine-owned agent instances and their endpoint/model selection.";
  };
}
