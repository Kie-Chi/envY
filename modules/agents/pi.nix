{ config, lib, piPackage, pkgs, ... }:

let
  piConfig = config.envy.agents.pi;
  prompts = import ./prompts.nix { inherit lib pkgs; };
  selectedPiPackage = if piConfig.moreThinking then
    import ./pi-package.nix { inherit piPackage pkgs; }
    else piPackage;
  providerName = "envy-pi";
  wrapper = import ./wrapper.nix { inherit lib pkgs; };
  endpointFor = agent: (wrapper.resolve config agent).endpoint;
  apiFor = agent: endpoint:
    if agent.api != null then agent.api
    else if endpoint != null && endpoint.protocol == "anthropic-compatible" then "anthropic-messages"
    else "openai-completions";
  isAnthropicApi = api: api == "anthropic-messages";
  templateNameFor = name: "envy-agent-${name}";
  templateFor = agent: let
    inherit (wrapper.resolve config agent) endpoint credentialName credential;
    api = apiFor agent endpoint;
  in ''
    ${lib.optionalString (endpoint != null && isAnthropicApi api) ''
    export ANTHROPIC_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
    ''}
    ${lib.optionalString (endpoint != null && !(isAnthropicApi api)) ''
    export OPENAI_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
    ''}
    ${lib.optionalString (credentialName != null && isAnthropicApi api) ''
    export ANTHROPIC_API_KEY=${lib.escapeShellArg credential}
    ''}
    ${lib.optionalString (credentialName != null && !(isAnthropicApi api)) ''
    export OPENAI_API_KEY=${lib.escapeShellArg credential}
    ''}
    # The image-PPT skill reads this selector from the caller environment.
    export GPT_IMAGE_MODEL_NAME=gpt-image-2
  '';
  modelConfigFor = agent: apiKey: let
    endpoint = endpointFor agent;
    api = apiFor agent endpoint;
    # Adaptive-thinking Anthropic models reject thinking.type=disabled, which
    # Pi sends for requests without a reasoning level (for example /btw).
    thinkingLevelMap = lib.optionalAttrs (api == "anthropic-messages") {
      off = null;
    } // lib.optionalAttrs (agent.adapter == "pi" && agent.moreThinking) {
      xhigh = "xhigh";
      max = "max";
      ultra = "ultra";
    };
  in {
    providers.${providerName} = {
      baseUrl = if endpoint == null then "https://api.openai.com/v1" else endpoint.baseUrl;
      inherit api;
      inherit apiKey;
      models = [
        ({
          id = agent.model;
          name = agent.model;
          reasoning = true;
          input = [ "text" "image" ];
        } // lib.optionalAttrs (api == "anthropic-messages") {
          compat = {
            forceAdaptiveThinking = true;
          };
        } // lib.optionalAttrs (agent.contextWindow != null) {
          contextWindow = agent.contextWindow;
        } // lib.optionalAttrs (agent.maxTokens != null) {
          maxTokens = agent.maxTokens;
        } // lib.optionalAttrs (thinkingLevelMap != { }) {
          inherit thinkingLevelMap;
        })
      ];
    };
  };
  piWrapper = wrapper.mkWrapper {
    name = "pi";
    executable = lib.getExe selectedPiPackage;
    envFiles = [
      config.sops.templates.${templateNameFor "pi"}.path
      (wrapper.runtimeEnvPath config.envy.user.home "pi")
    ];
    beforeEnv = ''
      # Pi dispatches management subcommands before parsing session options.
      case "''${1-}" in
        install|remove|uninstall|update|list|config|auth|mcp)
          exec ${lib.getExe selectedPiPackage} "$@"
          ;;
      esac
    '';
    beforeArgs = prompts.shellArgs "pi" + lib.optionalString (piConfig.model != null) ''

      if [[ -n "''${ENVY_AGENT_MODEL:-}" ]]; then
        cmd+=(--model "${providerName}/''${ENVY_AGENT_MODEL}")
      else
        cmd+=(--model ${lib.escapeShellArg "${providerName}/${piConfig.model}"})
      fi
    '';
    inherit (piConfig) args;
  };
in
{
  config = {
    sops.templates.${templateNameFor "pi"}.content = templateFor piConfig;

    agents.settingsFiles.pi = {
      target = ".pi/agent/settings.json";
      format = "json";
    };
    envy.agents.pi.runtimeFiles = [ {
      name = "models";
      target = ".pi/agent/models.json";
      format = "json";
      data = let
        endpoint = endpointFor piConfig;
        api = apiFor piConfig endpoint;
        apiKey = if isAnthropicApi api then "$ANTHROPIC_API_KEY" else "$OPENAI_API_KEY";
      in modelConfigFor piConfig apiKey;
    } ];
    envy.software.nix.packages.include = [ piWrapper ];
    envy.software.nix.packages.sourceDependencies = { pi = [ "nix:pi" ]; };
    envy.software.nix.packages.references = {
      pi = "local:modules/agents/pi.nix#pi";
    };
  };
}
