{ config, lib, pkgs, ... }:

let
  genericAgents = lib.filterAttrs
    (_: agent: builtins.elem agent.adapter [ "openai-compatible-cli" "stepgo" ])
    config.envy.agents;
  mkAgent = name: agent:
    let
      endpoint = if agent.endpoint == null
        || !(builtins.hasAttr agent.endpoint config.envy.llm.endpoints) then null
        else config.envy.llm.endpoints.${agent.endpoint};
      credentialName = if agent.credential == null then null
        else "llm-credential-${agent.credential}";
      credential = if credentialName == null || !(builtins.hasAttr credentialName config.sops.placeholder) then ""
        else builtins.getAttr credentialName config.sops.placeholder;
      command = if agent.command == null then name else agent.command;
      templateName = "envy-agent-${name}";
      wrapper = pkgs.writeShellApplication {
        inherit name;
        text = ''
          if [ -f "${config.sops.templates.${templateName}.path}" ]; then
            # shellcheck source=/dev/null
            source "${config.sops.templates.${templateName}.path}"
          fi
          if [ -f "${config.envy.user.home}/.config/envy/agent-runtime/${name}.env" ]; then
            # shellcheck source=/dev/null
            source "${config.envy.user.home}/.config/envy/agent-runtime/${name}.env"
          fi
          cmd=(${lib.escapeShellArg command})
          ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") agent.args}
          exec "''${cmd[@]}" "$@"
        '';
      };
      env = let
        prefix = if agent.adapter == "stepgo" then "STEPFUN" else "OPENAI";
      in lib.concatStringsSep "\n" (lib.filter (value: value != "") [
        (lib.optionalString (endpoint != null) "export ${prefix}_BASE_URL=${lib.escapeShellArg endpoint.baseUrl}")
        (lib.optionalString (credentialName != null) "export ${prefix}_API_KEY=${lib.escapeShellArg credential}")
        (lib.optionalString (agent.model != null) "export ${prefix}_MODEL=${lib.escapeShellArg agent.model}")
        "export GPT_IMAGE_MODEL_NAME=gpt-image-2"
      ]);
    in {
      package = wrapper;
      inherit templateName env;
      aliases = agent.aliases;
    };
  rendered = lib.mapAttrs mkAgent genericAgents;
  templates = lib.mapAttrs' (_: value: lib.nameValuePair value.templateName { content = value.env; }) rendered;
  packages = lib.attrValues (lib.mapAttrs (_: value: value.package) rendered);
  references = lib.mapAttrs (name: _: "local:modules/agents/generic.nix#${name}") rendered;
  aliases = lib.concatLists (lib.mapAttrsToList
    (name: value: map (alias: pkgs.writeShellScriptBin alias
      ("exec " + lib.escapeShellArg name + " \"$@\"")) value.aliases)
    rendered);
  aliasReferences = lib.listToAttrs (lib.concatLists (lib.mapAttrsToList
    (name: value: map (alias: lib.nameValuePair alias "local:modules/agents/generic.nix#${name}") value.aliases)
    rendered));
in
{
  config = {
    sops.templates = templates;
    envy.software.nix.packages.include = packages ++ aliases;
    envy.software.nix.packages.references = references // aliasReferences;
  };
}
