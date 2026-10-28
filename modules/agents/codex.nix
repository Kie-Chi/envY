{ config, lib, machinePlatform, pkgs, ... }:

let
  agentConfig = config.envy.agents.codex;
  prompts = import ./prompts.nix { inherit lib pkgs; };
  wrapper = import ./wrapper.nix { inherit lib pkgs; };
  inherit (wrapper.resolve config agentConfig) endpoint credentialName credential;
  templateName = "envy-agent-codex";
  codex = wrapper.mkWrapper {
    name = "codex";
    executable = lib.getExe pkgs.codex;
    envFiles = [
      config.sops.templates.${templateName}.path
      (wrapper.runtimeEnvPath config.envy.user.home "codex")
    ];
    beforeArgs = prompts.shellArgs "codex";
    args = agentConfig.args;
  };
in

{
  config = lib.mkMerge [
    { agents.settingsFiles.codex = {
      target = ".codex/config.toml";
      format = "toml";
    };
    # amux reads these credentials on every platform; the wrapper command
    # stays Linux-only because Darwin installs Codex as a Homebrew cask.
    sops.templates.${templateName}.content = ''
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
    ''; }
    (lib.mkIf (machinePlatform == "linux") {
    envy.software.nix.packages.include = [ codex ];
    envy.software.nix.packages.references.codex = "local:modules/agents/codex.nix#codex";
    })
  ];
}
