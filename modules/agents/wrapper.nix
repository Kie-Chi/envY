# Shared shape of agent command wrappers and their credential lookup.
{ lib, pkgs }:

{
  # Resolve an agent's endpoint and the sops placeholder of its credential.
  resolve = config: agent: let
    credentialName = if agent.credential == null then null
      else "llm-credential-${agent.credential}";
  in {
    endpoint = if agent.endpoint == null
      || !(builtins.hasAttr agent.endpoint config.envy.llm.endpoints) then null
      else config.envy.llm.endpoints.${agent.endpoint};
    inherit credentialName;
    credential = if credentialName == null
      || !(builtins.hasAttr credentialName config.sops.placeholder) then ""
      else builtins.getAttr credentialName config.sops.placeholder;
  };

  # Written by `envy agent` for changes that apply without activation.
  runtimeEnvPath = home: name: "${home}/.config/envy/agent-runtime/${name}.env";

  # Source each existing env file once, in order, so later files override
  # earlier ones, then run the agent with its fixed arguments ahead of the
  # caller's. The hooks run before the env files are read, before the fixed
  # arguments, and after them.
  mkWrapper = {
    name,
    executable,
    envFiles,
    args ? [ ],
    beforeEnv ? "",
    beforeArgs ? "",
    afterArgs ? ""
  }:
    pkgs.writeShellApplication {
      inherit name;
      text = ''
        ${beforeEnv}
        ${lib.concatMapStrings (file: ''
          if [ -f "${file}" ]; then
            # shellcheck source=/dev/null
            source "${file}"
          fi
        '') envFiles}
        cmd=(${lib.escapeShellArg executable})
        ${beforeArgs}
        ${lib.concatMapStringsSep "\n" (arg: "cmd+=(${lib.escapeShellArg arg})") args}
        ${afterArgs}
        exec "''${cmd[@]}" "$@"
      '';
    };

  mkAliases = name: map (alias: pkgs.writeShellScriptBin alias ''
    exec ${lib.escapeShellArg name} "$@"
  '');
}
