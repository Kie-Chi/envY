{ config, lib, pkgs, ... }:

let
  mirrors = (import ../../mirrors/resolve.nix { inherit lib; })
    (import ../../mirrors/catalog.nix).${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  prompts = import ../prompts.nix { inherit lib pkgs; };
  home = config.envy.user.home;
  # Container agent versions follow the nixpkgs lock. nixpkgs pins Claude Code
  # binaries for every platform in this manifest.
  claudeManifest = lib.importJSON (pkgs.path + "/pkgs/by-name/cl/claude-code/manifest.json");
  # amux owns the permission mode, so host wrapper flags are not inherited.
  permissionFlags = [ "--dangerously-skip-permissions" "--dangerously-bypass-approvals-and-sandbox" "--yolo" ];
  inheritedArgs = args: lib.filter (arg: !(builtins.elem arg permissionFlags)) args;
  templatePath = name:
    lib.optional (config.sops.templates ? ${name}) config.sops.templates.${name}.path;
  runtimeEnv = name: "${home}/.config/envy/agent-runtime/${name}.env";
  # The slim base image has no CA bundle before its first APT install; APT
  # still verifies the signed Release files.
  aptBootstrap = url: "http://" + lib.removePrefix "http://" (lib.removePrefix "https://" url);
  gitConfig = pkgs.writeText "amux-gitconfig" (lib.generators.toGitINI {
    user = {
      name = config.envy.git.name;
      email = config.envy.git.email;
    };
  });
  settings = pkgs.writeText "amux-config.json" (builtins.toJSON {
    user = config.envy.user.name;
    inherit home;
    git = lib.getExe pkgs.git;
    gitConfig = "${gitConfig}";
    skipPermissions = config.envy.amux.skipPermissions;
    image = {
      context = "${./image}";
      buildArgs = {
        BASE_IMAGE = "${mirrors.docker.registry}/library/debian:trixie-slim";
        APT_MIRROR = aptBootstrap mirrors.apt.debian;
        APT_SECURITY_MIRROR = aptBootstrap mirrors.apt.debianSecurity;
        NPM_REGISTRY = mirrors.npm.registry;
        CLAUDE_VERSION = claudeManifest.version;
        CLAUDE_SHA256_ARM64 = claudeManifest.platforms.linux-arm64.checksum;
        CLAUDE_SHA256_AMD64 = claudeManifest.platforms.linux-x64.checksum;
        CODEX_VERSION = pkgs.codex.version;
      };
    };
    agents = {
      claude = {
        command = "claude";
        skipArgs = [ "--dangerously-skip-permissions" ];
        args = inheritedArgs (config.agents.claude.extraArgs ++ config.envy.agents.cc.args)
          ++ prompts.argsFor "claude-code";
        envFiles = templatePath "envy-agent-cc" ++ [ (runtimeEnv "cc") ];
        # Claude Code asks about custom keys when ANTHROPIC_API_KEY is set.
        env = [ "ANTHROPIC_BASE_URL" "ANTHROPIC_AUTH_TOKEN" "ANTHROPIC_MODEL" ];
        skills = ".claude/skills";
        addDirFlag = "--add-dir";
      };
      codex = {
        command = "codex";
        skipArgs = [ "--dangerously-bypass-approvals-and-sandbox" ];
        # Codex's bubblewrap sandbox cannot create namespaces in the
        # container, which is already the sandbox; keep only the approvals.
        promptArgs = [ "--sandbox" "danger-full-access" "--ask-for-approval" "untrusted" ];
        args = [ "-c" "check_for_update_on_startup=false" ]
          ++ inheritedArgs config.envy.agents.codex.args
          ++ prompts.argsFor "codex";
        envFiles = templatePath "envy-agent-codex" ++ [ (runtimeEnv "codex") ];
        env = [ "OPENAI_BASE_URL" "OPENAI_API_KEY" "OPENAI_MODEL" ];
        skills = ".codex/skills";
      };
    };
  });
  amux = pkgs.writeShellApplication {
    name = "amux";
    checkPhase = ''
      PATH=${pkgs.git}/bin:$PATH AMUX_PATH=${./amux.py} \
        ${pkgs.python3}/bin/python3 ${../tests/test_amux.py}
    '';
    text = ''
      export AMUX_CONFIG=${settings}
      exec ${pkgs.python3}/bin/python3 -I ${./amux.py} "$@"
    '';
  };
in
{
  # amux runs Claude Code and Codex in detachable Docker sessions. Docker
  # itself (OrbStack on Darwin) is provided outside this module.
  assertions = [
    {
      assertion = claudeManifest.version == pkgs.claude-code.version;
      message = "amux expects the nixpkgs claude-code manifest at pkgs/by-name/cl/claude-code.";
    }
  ];

  envy.software.nix.packages.include = [ amux ];
  envy.software.nix.packages.references.amux = "local:modules/agents/amux/default.nix#amux";
}
