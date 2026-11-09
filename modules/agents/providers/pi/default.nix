{ config, lib, piPackage, pkgs, ... }:

let
  piConfig = config.envy.agents.pi;
  prompts = import ../../prompts.nix { inherit lib pkgs; };
  selectedPiPackage = import ./package.nix {
    inherit lib piPackage;
    inherit (piConfig) moreThinking;
  };
  selections = import ./models.nix { inherit lib; } {
    agent = piConfig;
    profiles = config.envy.agentProfiles or {};
    endpoints = config.envy.llm.endpoints;
    credentials = config.envy.llm.credentials or {};
  };
  defaultSelection = selections.default;
  defaultAgent = piConfig // defaultSelection.selection;
  wrapper = import ../../wrapper.nix { inherit lib pkgs; };
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
  modelConfigFor = providerName: agent: apiKey: let
    endpoint = endpointFor agent;
    api = apiFor agent endpoint;
    isSolModel = builtins.elem api [ "openai-completions" "openai-responses" ]
      && agent.model != null
      && (agent.model == "gpt-6.1-sol" || lib.hasPrefix "gpt-6.1-sol-" agent.model);
    # Adaptive-thinking Anthropic models reject thinking.type=disabled, which
    # Pi sends for requests without a reasoning level (for example /btw).
    thinkingLevelMap = lib.optionalAttrs (api == "anthropic-messages") {
      off = null;
    } // lib.optionalAttrs (agent.adapter == "pi" && agent.moreThinking) {
      xhigh = "xhigh";
      max = "max";
      ultra = "ultra";
    } // lib.optionalAttrs isSolModel {
      # Sol accepts low through max, not none, minimal, or ultra. Null entries
      # omit the default effort and clamp explicit unsupported requests.
      off = null;
      minimal = null;
      ultra = null;
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
      unset ENVY_AGENT_MODEL ENVY_AGENT_PROVIDER
    '';
    beforeArgs = prompts.shellArgs "pi" + "\n" + ''
      if [[ -n "''${ENVY_AGENT_MODEL:-}" ]]; then
        cmd+=(--model "''${ENVY_AGENT_PROVIDER:-envy-pi}/''${ENVY_AGENT_MODEL}")
      ${lib.optionalString (defaultAgent.model != null) ''
      else
        cmd+=(--model ${lib.escapeShellArg "${defaultSelection.provider}/${defaultAgent.model}"})
      ''}
      fi
    '';
    inherit (piConfig) args;
  };
in
{
  imports = [ ./extensions.nix ./subagents.nix ./diagnostics.nix ];

  config = {
    sops.templates.${templateNameFor "pi"}.content = templateFor defaultAgent
      + lib.concatMapStrings (entry: let
        resolved = wrapper.resolve config entry.selection;
      in lib.optionalString (selections.multi && resolved.credentialName != null) ''
        export ${entry.keyVariable}=${lib.escapeShellArg resolved.credential}
      '') (lib.unique (map (entry: {
        inherit (entry) keyVariable;
        selection = { endpoint = null; credential = entry.selection.credential; };
      }) selections.bindings));

    # Activation supersedes envY's live overrides, not Pi-owned state.
    home.activation.resetPiRuntime = lib.hm.dag.entryAfter [ "mergeAgentRuntime-pi-models" ] ''
      if [ -z "$DRY_RUN_CMD" ]; then
        rm -f "$HOME/.config/envy/agent-runtime/pi.env"
      fi
    '';

    agents.settingsFiles.pi = {
      target = ".pi/agent/settings.json";
      format = "json";
    };
    envy.agents.pi.runtimeFiles = [ {
      name = "models";
      target = ".pi/agent/models.json";
      format = "json";
      managedKeyPrefixes = { providers = "envy-pi"; };
      data.providers = builtins.foldl' (acc: entry: let
        generated = (modelConfigFor entry.provider (piConfig // entry.selection) entry.apiKey).providers.${entry.provider};
        previous = acc.${entry.provider} or (generated // { models = []; });
      in acc // { ${entry.provider} = previous // { models = previous.models ++ generated.models; }; }
      ) {} selections.bindings;
    } ];
    envy.software.nix.packages.include = [ piWrapper ];
    envy.software.nix.packages.sourceDependencies = { pi = [ "nix:pi" ]; };
    envy.software.nix.packages.references = {
      pi = "local:modules/agents/providers/pi/default.nix#pi";
    };
  };
}
