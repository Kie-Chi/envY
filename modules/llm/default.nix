{ config, lib, ... }:

let
  configuredCredentials = lib.mapAttrs'
    (name: credential: lib.nameValuePair "llm-credential-${name}" {
      sopsFile = ../../secrets/secrets.yaml;
      key = credential.secretPath;
    }) config.envy.llm.credentials;
in

{
  imports = [ ./options.nix ];

  # Keep the two historical names while machines migrate to logical
  # credentials. Existing Raycast/Zotero consumers still use these names.
  sops.secrets = {
    llm-steps-apikey = {
      sopsFile = ../../secrets/secrets.yaml;
      key = "llm/steps/apikey";
    };
    llm-deepseek-apikey = {
      sopsFile = ../../secrets/secrets.yaml;
      key = "llm/deepseek/apikey";
    };
  } // configuredCredentials;

  # Compatibility artifact for older user-owned launchers. New agent adapters
  # use per-agent templates and the shell no longer sources this globally.
  sops.templates."env-secrets".content = ''
    export STEPFUN_BASE_URL=${lib.escapeShellArg config.envy.llm.steps.url}
    export API_KEY=${lib.escapeShellArg config.sops.placeholder.llm-steps-apikey}
    export STEPFUN_API_KEY=${lib.escapeShellArg config.sops.placeholder.llm-steps-apikey}
    export ANTHROPIC_BASE_URL=${lib.escapeShellArg config.envy.llm.steps.url}
    export ANTHROPIC_API_KEY=${lib.escapeShellArg config.sops.placeholder.llm-steps-apikey}
    export ANTHROPIC_AUTH_TOKEN=${lib.escapeShellArg config.sops.placeholder.llm-steps-apikey}
    export ANTHROPIC_MODEL=${lib.escapeShellArg config.envy.llm.steps.model}
  '';
}
