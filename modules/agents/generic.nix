{ config, lib, pkgs, ... }:

let
  genericAgents = lib.filterAttrs
    (_: agent: builtins.elem agent.adapter [ "openai-compatible-cli" "stepgo" ])
    config.envy.agents;
  wrapper = import ./wrapper.nix { inherit lib pkgs; };
  mkAgent = name: agent:
    let
      inherit (wrapper.resolve config agent) endpoint credentialName credential;
      command = if agent.command == null then name else agent.command;
      templateName = "envy-agent-${name}";
      package = wrapper.mkWrapper {
        inherit name;
        executable = command;
        envFiles = [
          config.sops.templates.${templateName}.path
          (wrapper.runtimeEnvPath config.envy.user.home name)
        ];
        inherit (agent) args;
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
      inherit package templateName env;
      aliases = agent.aliases;
    };
  rendered = lib.mapAttrs mkAgent genericAgents;
  templates = lib.mapAttrs' (_: value: lib.nameValuePair value.templateName { content = value.env; }) rendered;
  packages = lib.attrValues (lib.mapAttrs (_: value: value.package) rendered);
  references = lib.mapAttrs (name: _: "local:modules/agents/generic.nix#${name}") rendered;
  aliases = lib.concatLists (lib.mapAttrsToList
    (name: value: wrapper.mkAliases name value.aliases)
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
