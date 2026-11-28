{ config, lib, pkgs, ... }:

let
  prompts = import ../prompts.nix { inherit lib pkgs; };
  wrapper = import ../wrapper.nix { inherit lib pkgs; };
  versions = import ../providers/versions.nix { inherit lib pkgs; };
  codexPackage = import ../providers/agent-package.nix {
    inherit lib pkgs;
    kind = "codex";
    spec = versions.codex;
  };
  # Linux gets the envY wrapper around its independently locked npm release,
  # reading the template declared by providers/codex.nix.
  codex = wrapper.mkWrapper {
    name = "codex";
    executable = lib.getExe codexPackage;
    envFiles = [
      config.sops.templates.envy-agent-codex.path
      (wrapper.runtimeEnvPath config.envy.user.home "codex")
    ];
    beforeArgs = prompts.shellArgs "codex";
    inherit (config.envy.agents.codex) args;
  };
in
{
  envy.software.nix.packages.include = [ codex ];
  envy.software.nix.packages.references.codex = "local:modules/agents/linux/codex.nix#codex";
}
