{ config, lib, pkgs, ... }:

let
  agentConfig = config.envy.agents.cc;
  legacyConfig = config.agents.claude;
  packageEnabled = !(builtins.elem "claude" config.envy.software.nix.packages.exclude);
  promptFile = ".config/ccli/prompt";
  prompts = import ./prompts.nix { inherit lib pkgs; };
  systemPrompt = prompts.forAdapter "claude-code";
  endpoint = if agentConfig.endpoint == null
    || !(builtins.hasAttr agentConfig.endpoint config.envy.llm.endpoints) then null
    else config.envy.llm.endpoints.${agentConfig.endpoint};
  credentialName = if agentConfig.credential == null then null
    else "llm-credential-${agentConfig.credential}";
  credential = if credentialName == null || !(builtins.hasAttr credentialName config.sops.placeholder) then ""
    else builtins.getAttr credentialName config.sops.placeholder;
  templateName = "envy-agent-cc";
  agentArgs = legacyConfig.extraArgs ++ agentConfig.args;
  aliases = lib.unique ([ ] ++ agentConfig.aliases ++ lib.optional legacyConfig.ccliAlias "ccli");

  claudeWithDefaults = pkgs.writeShellApplication {
    name = "claude";
    text = ''
      system_prompt=${lib.escapeShellArg systemPrompt}
      prompt_file="$HOME/${promptFile}"

      if [[ -n "''${CCLI_SYSTEM_PROMPT:-}" ]]; then
        system_prompt="$CCLI_SYSTEM_PROMPT"
      elif [[ -f "$prompt_file" ]]; then
        system_prompt=$(<"$prompt_file")
      fi

      # Call nixpkgs' wrapper by absolute path. It prepares Claude Code's
      # runtime before handing off to its internal .claude-wrapped binary.
      if [ -f "${config.sops.templates.${templateName}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateName}.path}"
      fi
      if [ -f "${config.envy.user.home}/.config/envy/agent-runtime/cc.env" ]; then
        # shellcheck source=/dev/null
        source "${config.envy.user.home}/.config/envy/agent-runtime/cc.env"
      fi

      cmd=("${lib.getExe legacyConfig.package}")
      ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") agentArgs}

      if [[ -n "$system_prompt" ]]; then
        cmd+=(--system-prompt "$system_prompt")
      fi

      exec "''${cmd[@]}" "$@"
    '';
  };

  aliasesPackages = map (alias: pkgs.writeShellScriptBin alias ''
    exec claude "$@"
  '') aliases;
in
{
  options.agents.claude = {
    package = lib.mkPackageOption pkgs "claude-code" { };

    extraArgs = lib.mkOption {
      type = with lib.types; listOf str;
      default = [ ];
      description = "Arguments injected before caller-provided Claude arguments.";
    };

    ccliAlias = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Keep ccli as a compatibility command that delegates to claude.";
    };
  };

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
      claude = "local:modules/agents/claude.nix#claudeWithDefaults";
    } // lib.listToAttrs (map (alias: lib.nameValuePair alias "local:modules/agents/claude.nix#${alias}") aliases);

    home.file.${promptFile}.text = systemPrompt;
    })
  ];
}
