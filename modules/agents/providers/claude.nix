{ config, lib, pkgs, ... }:

let
  agentConfig = config.envy.agents.cc;
  packageEnabled = !(builtins.elem "claude" config.envy.software.nix.packages.exclude);
  promptFile = ".config/ccli/prompt";
  prompts = import ../prompts.nix { inherit lib pkgs; };
  systemPrompt = prompts.forAdapter "claude-code";
  wrapper = import ../wrapper.nix { inherit lib pkgs; };
  inherit (wrapper.resolve config agentConfig) endpoint credentialName credential;
  templateName = "envy-agent-cc";
  aliases = lib.unique agentConfig.aliases;
  versions = import ./versions.nix { inherit lib pkgs; };
  claudePackage = if pkgs.stdenv.hostPlatform.isLinux
    then import ./agent-package.nix { inherit lib pkgs; kind = "claude"; spec = versions.claude; }
    else pkgs.claude-code;

  claudeWithDefaults = wrapper.mkWrapper {
    name = "claude";
    # Linux uses envY's fast, independently locked npm release channel.
    # Darwin keeps the existing nixpkgs package until its native distribution
    # policy is migrated separately.
    executable = lib.getExe claudePackage;
    envFiles = [
      config.sops.templates.${templateName}.path
      (wrapper.runtimeEnvPath config.envy.user.home "cc")
    ];
    args = agentConfig.args;
    beforeEnv = ''
      system_prompt=${lib.escapeShellArg systemPrompt}
      prompt_file="$HOME/${promptFile}"

      if [[ -n "''${CCLI_SYSTEM_PROMPT:-}" ]]; then
        system_prompt="$CCLI_SYSTEM_PROMPT"
      elif [[ -f "$prompt_file" ]]; then
        system_prompt=$(<"$prompt_file")
      fi
    '';
    afterArgs = ''
      if [[ -n "$system_prompt" ]]; then
        cmd+=(--system-prompt "$system_prompt")
      fi
    '';
  };

  aliasesPackages = wrapper.mkAliases "claude" aliases;
in
{
  config = lib.mkMerge [
    { agents.settingsFiles.claude-code = {
      target = ".claude/settings.json";
      format = "json";
    }; }
    (lib.mkIf packageEnabled {
    sops.templates.${templateName}.content = ''
      ${lib.optionalString (endpoint != null) ''
      export ANTHROPIC_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
      ''}
      ${lib.optionalString (credentialName != null) ''
      export ANTHROPIC_API_KEY=${lib.escapeShellArg credential}
      export ANTHROPIC_AUTH_TOKEN=${lib.escapeShellArg credential}
      ''}
      ${lib.optionalString (agentConfig.model != null) ''
      export ANTHROPIC_MODEL=${lib.escapeShellArg agentConfig.model}
      ''}
      export GPT_IMAGE_MODEL_NAME=gpt-image-2
    '';
    envy.software.nix.packages.include = [
      claudeWithDefaults
    ] ++ aliasesPackages;
    envy.software.nix.packages.references = {
      claude = "local:modules/agents/providers/claude.nix#claudeWithDefaults";
    } // lib.listToAttrs (map (alias: lib.nameValuePair alias "local:modules/agents/providers/claude.nix#${alias}") aliases);

    home.file.${promptFile}.text = systemPrompt;
    })
  ];
}
