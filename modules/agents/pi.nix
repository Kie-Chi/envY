{ config, lib, ohMyPiPackage, piPackage, pkgs, ... }:

let
  piConfig = config.envy.agents.pi;
  ompConfig = config.envy.agents.omp;
  providerName = "envy-pi";
  endpoint = if piConfig.endpoint == null
    || !(builtins.hasAttr piConfig.endpoint config.envy.llm.endpoints) then null
    else config.envy.llm.endpoints.${piConfig.endpoint};
  credentialName = if piConfig.credential == null
    then null
    else "llm-credential-${piConfig.credential}";
  credential = if credentialName == null
    || !(builtins.hasAttr credentialName config.sops.placeholder)
    then ""
    else builtins.getAttr credentialName config.sops.placeholder;
  templateName = "envy-agent-pi";
  model = piConfig.model;
  modelConfig = {
    providers = {
      ${providerName} = {
        baseUrl = if endpoint == null then "https://api.openai.com/v1" else endpoint.baseUrl;
        api = "openai-completions";
        apiKey = "$OPENAI_API_KEY";
        models = [
          {
            id = model;
            name = model;
            reasoning = true;
            input = [ "text" "image" ];
          }
        ];
      };
    };
  };
  piWrapper = pkgs.writeShellApplication {
    name = "pi";
    text = ''
      if [ -f "${config.sops.templates.${templateName}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateName}.path}"
      fi
      cmd=(${lib.escapeShellArg (lib.getExe piPackage)})
      ${lib.optionalString (model != null) ''
      cmd+=(--model ${lib.escapeShellArg "${providerName}/${model}"})
      ''}
      ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") piConfig.args}
      exec "''${cmd[@]}" "$@"
    '';
  };
  ompWrapper = pkgs.writeShellApplication {
    name = "omp";
    text = ''
      if [ -f "${config.sops.templates.${templateName}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateName}.path}"
      fi
      cmd=(${lib.escapeShellArg (lib.getExe ohMyPiPackage)})
      ${lib.optionalString (ompConfig.model != null) ''
      cmd+=(--model ${lib.escapeShellArg "${providerName}/${ompConfig.model}"})
      ''}
      ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") ompConfig.args}
      exec "''${cmd[@]}" "$@"
    '';
  };
in
{
  config = {
    sops.templates.${templateName}.content = ''
      ${lib.optionalString (endpoint != null) ''
      export OPENAI_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
      ''}
      ${lib.optionalString (credentialName != null) ''
      export OPENAI_API_KEY=${lib.escapeShellArg credential}
      ''}
      # The image-PPT skill reads this selector from the caller environment.
      export GPT_IMAGE_MODEL_NAME=gpt-image-2
    '';

    # models.json is routing metadata only. It contains an environment
    # variable reference, never the decrypted API key.
    home.file.".pi/agent/models.json".text = builtins.toJSON modelConfig;
    home.file.".omp/agent/models.yml".text = builtins.toJSON modelConfig;

    envy.software.nix.packages.include = [ piWrapper ompWrapper ];
    envy.software.nix.packages.references = {
      pi = "local:modules/agents/pi.nix#pi";
      omp = "local:modules/agents/pi.nix#omp";
    };
  };
}
