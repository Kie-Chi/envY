{ config, lib, pkgs, ... }:

let
  profiles = config.envy.agentProfiles;
  wrapper = import ../wrapper.nix { inherit lib pkgs; };

  # Keep this override order identical to envy/agent.py: a named profile
  # replaces every model selection field, including explicit nulls.
  resolveAgent = agent:
    let profile = agent.profile; in
    if profile != null && builtins.hasAttr profile profiles then
      agent // {
        endpoint = profiles.${profile}.endpoint or null;
        credential = profiles.${profile}.credential or null;
        api = profiles.${profile}.api or null;
        model = profiles.${profile}.model or null;
        contextWindow = profiles.${profile}.contextWindow or null;
        maxTokens = profiles.${profile}.maxTokens or null;
      }
    else agent;

  codexAgent = config.envy.agents.codex;
  resolvedAgent = resolveAgent codexAgent;

  # envY owns this provider namespace so activation can replace it without
  # touching user-defined providers in Codex configuration.
  providerId = endpointName: "envy-${endpointName}";

  wireApi = agent: endpoint:
    if agent.api == "openai-completions" then "chat"
    else if agent.api == "openai-responses" then "responses"
    else if agent.api == "anthropic-messages"
      || (agent.api == null && endpoint != null && endpoint.protocol == "anthropic-compatible")
      then throw "Codex cannot use an Anthropic wire API; select an OpenAI-compatible endpoint."
    else null;

  # Render one instance into Codex config.toml shape. runtime-files.nix merges
  # every codex instance into one file before machine additionalSettings.
  settingsFor = agent:
    let
      resolved = resolveAgent agent;
      inherit (wrapper.resolve config resolved) endpoint credentialName;
      endpointName = resolved.endpoint;
      api = wireApi resolved endpoint;
    in
      lib.optionalAttrs (endpoint != null && endpointName != null && resolved.model != null) ({
        model = resolved.model;
        model_provider = providerId endpointName;
        model_providers.${providerId endpointName} =
          {
            name = endpointName;
            base_url = endpoint.baseUrl;
          }
          // lib.optionalAttrs (api != null) { wire_api = api; }
          // lib.optionalAttrs (credentialName != null) { requires_openai_auth = true; };
      }
      // lib.optionalAttrs ((resolved.contextWindow or null) != null) { model_context_window = resolved.contextWindow; }
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
    home.activation = lib.optionalAttrs (authSecret != null) {
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
