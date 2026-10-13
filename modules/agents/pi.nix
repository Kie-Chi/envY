{ config, lib, ohMyPiPackage, piPackage, pkgs, ... }:

let
  piConfig = config.envy.agents.pi;
  ompConfig = config.envy.agents.omp;
  selectedPiPackage = if piConfig.more_thinking then
    import ./pi-package.nix { inherit piPackage pkgs; }
    else piPackage;
  providerName = "envy-pi";
  endpointFor = agent: if agent.endpoint == null
    || !(builtins.hasAttr agent.endpoint config.envy.llm.endpoints) then null
    else config.envy.llm.endpoints.${agent.endpoint};
  credentialNameFor = agent: if agent.credential == null
    then null
    else "llm-credential-${agent.credential}";
  templateNameFor = name: "envy-agent-${name}";
  templateFor = agent: let
    endpoint = endpointFor agent;
    credentialName = credentialNameFor agent;
    credential = if credentialName == null
      || !(builtins.hasAttr credentialName config.sops.placeholder)
      then ""
      else builtins.getAttr credentialName config.sops.placeholder;
  in ''
    ${lib.optionalString (endpoint != null) ''
    export OPENAI_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}
    ''}
    ${lib.optionalString (credentialName != null) ''
    export OPENAI_API_KEY=${lib.escapeShellArg credential}
    ''}
    # The image-PPT skill reads this selector from the caller environment.
    export GPT_IMAGE_MODEL_NAME=gpt-image-2
  '';
  modelConfigFor = agent: apiKey: let
    endpoint = endpointFor agent;
  in {
    providers.${providerName} = {
      baseUrl = if endpoint == null then "https://api.openai.com/v1" else endpoint.baseUrl;
      api = agent.api;
      inherit apiKey;
      models = [
        ({
          id = agent.model;
          name = agent.model;
          reasoning = true;
          input = [ "text" "image" ];
        } // lib.optionalAttrs (agent.adapter == "pi" && agent.more_thinking) {
          thinkingLevelMap = {
            xhigh = "xhigh";
            max = "max";
            ultra = "ultra";
          };
        })
      ];
    };
  };
  piWrapper = pkgs.writeShellApplication {
    name = "pi";
    text = ''
      if [ -f "${config.sops.templates.${templateNameFor "pi"}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateNameFor "pi"}.path}"
      fi
      cmd=(${lib.escapeShellArg (lib.getExe selectedPiPackage)})
      ${lib.optionalString (piConfig.model != null) ''
      cmd+=(--model ${lib.escapeShellArg "${providerName}/${piConfig.model}"})
      ''}
      ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") piConfig.args}
      exec "''${cmd[@]}" "$@"
    '';
  };
  ompWrapper = pkgs.writeShellApplication {
    name = "omp";
    text = ''
      if [ -f "${config.sops.templates.${templateNameFor "omp"}.path}" ]; then
        # shellcheck source=/dev/null
        source "${config.sops.templates.${templateNameFor "omp"}.path}"
      fi
      cmd=(${lib.escapeShellArg (lib.getExe ohMyPiPackage)})
      ${lib.optionalString (credentialNameFor ompConfig != null) ''
      cmd+=(--api-key "$OPENAI_API_KEY")
      ''}
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
    sops.templates.${templateNameFor "pi"}.content = templateFor piConfig;
    sops.templates.${templateNameFor "omp"}.content = templateFor ompConfig;

    # Model files contain credential references, never decrypted API keys.
    home.file.".pi/agent/models.json".text = builtins.toJSON
      (modelConfigFor piConfig "$OPENAI_API_KEY");
    # OMP validates custom providers before applying CLI credentials. Its
    # command-backed apiKey satisfies that validation without embedding the
    # decrypted secret in the generated model file.
    home.file.".omp/agent/models.yml".text = builtins.toJSON
      (modelConfigFor ompConfig "!printf '%s' \"$OPENAI_API_KEY\"");

    envy.software.nix.packages.include = [ piWrapper ompWrapper ];
    envy.software.nix.packages.sourceDependencies = { pi = [ "nix:pi" ]; omp = [ "nix:oh-my-pi" ]; };
    envy.software.nix.packages.references = {
      pi = "local:modules/agents/pi.nix#pi";
      omp = "local:modules/agents/pi.nix#omp";
    };
  };
}
