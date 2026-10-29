{ config, lib, pkgs, ... }:

let
  agentConfig = config.envy.agents.codex;
  wrapper = import ../wrapper.nix { inherit lib pkgs; };
  inherit (wrapper.resolve config agentConfig) endpoint credentialName credential;
in

{
  config = {
    agents.settingsFiles.codex = {
      target = ".codex/config.toml";
      format = "toml";
    };
    # amux reads these credentials on every platform; the wrapper command is
    # Linux-only (linux/codex.nix) because Darwin installs Codex as a cask.
    sops.templates.envy-agent-codex.content = ''
      ${lib.optionalString (endpoint != null) ''
      export OPENAI_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
      ''}
      ${lib.optionalString (credentialName != null) ''
      export OPENAI_API_KEY=${lib.escapeShellArg credential}
      ''}
      ${lib.optionalString (agentConfig.model != null) ''
      export OPENAI_MODEL=${lib.escapeShellArg agentConfig.model}
      ''}
      export GPT_IMAGE_MODEL_NAME=gpt-image-2
    '';
  };
}
