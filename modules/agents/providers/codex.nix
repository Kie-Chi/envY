{ config, lib, pkgs, ... }:

let
  profiles = config.envy.agentProfiles;
  wrapper = import ../wrapper.nix { inherit lib pkgs; };

  selectionsFor = agent: import ./codex/models.nix { inherit lib; } {
    inherit agent profiles;
    inherit (config.envy.llm) endpoints credentials;
  };
  resolveAgent = agent: agent // (selectionsFor agent).default.selection;
  catalogTarget = "/.codex/envy-models.json";
  catalogSelections = map (entry: entry.selection) (selectionsFor config.envy.agents.codex).bindings;
  catalogSource = pkgs.writeText "codex-model-selections.json" (builtins.toJSON catalogSelections);
  codexExecutable = if pkgs.stdenv.hostPlatform.isDarwin then "/opt/homebrew/bin/codex" else lib.getExe pkgs.codex;

  codexAgent = config.envy.agents.codex;
  resolvedAgent = resolveAgent codexAgent;

  # envY owns this provider namespace so activation can replace it without
  # touching user-defined providers in Codex configuration.
  providerId = endpointName: "envy-${endpointName}";

  # Render one instance into Codex config.toml shape. runtime-files.nix merges
  # every codex instance into one file before machine additionalSettings.
  settingsFor = agent:
    let
      resolved = resolveAgent agent;
      inherit (wrapper.resolve config resolved) endpoint credentialName;
      endpointName = resolved.endpoint;
    in
      lib.optionalAttrs (endpoint != null && endpointName != null && resolved.model != null) ({
        model = resolved.model;
        model_provider = providerId endpointName;
        model_providers.${providerId endpointName} =
          {
            name = endpointName;
            base_url = endpoint.baseUrl;
          }
          // { wire_api = "responses"; }
          // lib.optionalAttrs (credentialName != null) { requires_openai_auth = true; };
      }
      // lib.optionalAttrs ((agent.models or []) != []) { model_catalog_json = config.envy.user.home + catalogTarget; }
      // lib.optionalAttrs ((agent.models or []) == [] && (resolved.contextWindow or null) != null) { model_context_window = resolved.contextWindow; }
      // lib.optionalAttrs ((resolved.maxTokens or null) != null) { model_max_output_tokens = resolved.maxTokens; });

  authSecret =
    let inherit (wrapper.resolve config resolvedAgent) credentialName; in
    if credentialName != null && builtins.hasAttr credentialName config.sops.secrets
      then config.sops.secrets.${credentialName}.path
      else null;
in

{
  config = {
    agents.settingsFiles.codex = {
      target = ".codex/config.toml";
      format = "toml";
      aggregate = true;
      managedKeyPrefixes.model_providers = "envy";
      managedKeys = [ "model_catalog_json" "model_context_window" "model_max_output_tokens" ];
      render = settingsFor;
    };

    # amux reads these credentials on every platform; the wrapper command is
    # Linux-only (linux/codex.nix) because Darwin installs Codex as a cask.
    sops.templates.envy-agent-codex.content = let
      inherit (wrapper.resolve config resolvedAgent) endpoint credentialName credential;
    in ''
      ${lib.optionalString (endpoint != null) ''
      export OPENAI_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
      ''}
      ${lib.optionalString (credentialName != null) ''
      export OPENAI_API_KEY=${lib.escapeShellArg credential}
      ''}
      ${lib.optionalString (resolvedAgent.model != null) ''
      export OPENAI_MODEL=${lib.escapeShellArg resolvedAgent.model}
      ''}
      export GPT_IMAGE_MODEL_NAME=gpt-image-2
    '';

    # Codex reads its API credential from auth.json, not from OPENAI_API_KEY.
    home.activation = lib.optionalAttrs ((codexAgent.models or []) != []) {
      mergeCodexModels = lib.hm.dag.entryBetween [ "mergeAgentRuntime-codex-settings" ] [ "linkGeneration" ] ''
        if [ -z "$DRY_RUN_CMD" ]; then
          PYTHONPATH=${../../../resources/scripts} ${pkgs.python3}/bin/python -m envy.agent_codex \
            --source ${catalogSource} --target "$HOME"${lib.escapeShellArg catalogTarget} \
            --codex ${lib.escapeShellArg codexExecutable}
        fi
      '';
    } // lib.optionalAttrs (authSecret != null) {
      mergeCodexAuth = lib.hm.dag.entryAfter [ "userBoundary" ] ''
        _LOG_CTX="mergeCodexAuth"
        if [ -z "$DRY_RUN_CMD" ] && [ -f ${lib.escapeShellArg authSecret} ]; then
          PYTHONPATH=${../../../resources/scripts} ${pkgs.python3}/bin/python -m envy.agent_auth \
            --target "$HOME/.codex/auth.json" --secret-file ${lib.escapeShellArg authSecret} \
            || log_warn "Codex auth.json was not updated"
        fi
      '';
    };
  };
}
