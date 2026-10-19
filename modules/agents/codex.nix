{ config, lib, machinePlatform, pkgs, ... }:

let
  agentConfig = config.envy.agents.codex;
  prompts = import ./prompts.nix { inherit lib pkgs; };
  endpoint = if agentConfig.endpoint == null
    || !(builtins.hasAttr agentConfig.endpoint config.envy.llm.endpoints) then null
    else config.envy.llm.endpoints.${agentConfig.endpoint};
  credentialName = if agentConfig.credential == null then null
    else "llm-credential-${agentConfig.credential}";
  credential = if credentialName == null || !(builtins.hasAttr credentialName config.sops.placeholder) then ""
    else builtins.getAttr credentialName config.sops.placeholder;
  templateName = "envy-agent-codex";
  codex = pkgs.writeShellApplication {
    name = "codex";
    text = ''
      if [ -f "${config.sops.templates.${templateName}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateName}.path}"
      fi
      if [ -f "${config.envy.user.home}/.config/envy/agent-runtime/codex.env" ]; then
        # shellcheck source=/dev/null
        source "${config.envy.user.home}/.config/envy/agent-runtime/codex.env"
      fi
      cmd=(${lib.escapeShellArg (lib.getExe pkgs.codex)})
      ${prompts.shellArgs "codex"}
      ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") agentConfig.args}
      exec "''${cmd[@]}" "$@"
    '';
  };
in

{
  config = lib.mkMerge [
    { agents.settingsFiles.codex = {
      target = ".codex/config.toml";
      format = "toml";
    }; }
    (lib.mkIf (machinePlatform == "linux") {
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
    '';
    envy.software.nix.packages.include = [ codex ];
    envy.software.nix.packages.references.codex = "local:modules/agents/codex.nix#codex";
    })
  ];
}
