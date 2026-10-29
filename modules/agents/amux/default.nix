{ config, lib, pkgs, ... }:

let
  mirrors = (import ../../mirrors/resolve.nix { inherit lib; })
    (import ../../mirrors/catalog.nix).${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  prompts = import ../prompts.nix { inherit lib pkgs; };
  home = config.envy.user.home;
  python = "${pkgs.python3}/bin/python3";

  # Containers run Linux on the host CPU (OrbStack or Docker Desktop on Darwin).
  linuxSystem = "${pkgs.stdenv.hostPlatform.parsed.cpu.name}-linux";
  # A plain nixpkgs instance keeps store paths identical to the binary cache.
  linuxPkgs = import pkgs.path {
    localSystem.system = linuxSystem;
    config = { };
    overlays = [ ];
  };
  # Tools on every session's PATH. The host only evaluates their store paths;
  # containers substitute them into the shared amux-nix volume.
  tools = with linuxPkgs; [ fd file gnumake jq nodejs python3 tree unzip uv ];
  toolPaths = map (pkg: builtins.unsafeDiscardStringContext (lib.getBin pkg).outPath) tools;
  # "nixpkgs" inside containers resolves to the same locked revision.
  lock = lib.importJSON ../../../flake.lock;
  nixpkgsLock = lock.nodes.${lock.nodes.root.inputs.nixpkgs}.locked;

  etc = pkgs.runCommand "amux-etc" {
    passAsFile = [ "gitconfig" "nixconf" "registry" ];
    # Only the identity: host credential helpers and includes are host paths.
    gitconfig = lib.generators.toGitINI {
      user = {
        name = config.envy.git.name;
        email = config.envy.git.email;
      };
    };
    nixconf = ''
      experimental-features = nix-command flakes
      substituters = ${lib.concatStringsSep " " mirrors.nix.containerSubstituters}
      flake-registry = /amux/etc/registry.json
      fallback = true
      # The container is the sandbox; Nix cannot create namespaces inside it.
      sandbox = false
    '';
    registry = builtins.toJSON {
      version = 2;
      flakes = [
        {
          from = { type = "indirect"; id = "nixpkgs"; };
          to = { inherit (nixpkgsLock) type owner repo rev narHash; };
        }
      ];
    };
  } ''
    mkdir $out
    cp "$gitconfigPath" $out/gitconfig
    cp "$nixconfPath" $out/nix.conf
    cp "$registryPath" $out/registry.json
  '';

  imageContext = pkgs.runCommand "amux-image" { } ''
    cp -r ${./image} $out
    chmod -R u+w $out
    mkdir $out/nix-bin
    for tool in ${linuxPkgs.nixStatic}/bin/*; do
      name=$(basename "$tool")
      if [ "$name" = nix ]; then
        cp -L "$tool" $out/nix-bin/nix
      else
        ln -s nix "$out/nix-bin/$name"
      fi
    done
  '';

  # amux owns the permission mode, so host wrapper flags are not inherited.
  permissionFlags = [ "--dangerously-skip-permissions" "--dangerously-bypass-approvals-and-sandbox" "--yolo" ];
  inheritedArgs = args: lib.filter (arg: !(builtins.elem arg permissionFlags)) args;
  templatePath = name:
    lib.optional (config.sops.templates ? ${name}) config.sops.templates.${name}.path;
  runtimeEnv = (import ../wrapper.nix { inherit lib pkgs; }).runtimeEnvPath home;
  # The host's skill links point into its own store, which containers do not
  # see; mount each selected skill directory where the agent discovers it.
  # Reading home.file instead would make this package depend on itself.
  skillMounts = instance: dir: lib.listToAttrs (map
    (name: lib.nameValuePair "${home}/${dir}/${name}" "${config.agents.skills.catalog.${name}.source}")
    config.agents.skills.effective.${instance});
  # The slim base image has no CA bundle before its first APT install; APT
  # still verifies the signed Release files.
  aptBootstrap = url: "http://" + lib.removePrefix "http://" (lib.removePrefix "https://" url);

  settings = pkgs.writeText "amux-config.json" (builtins.toJSON {
    user = config.envy.user.name;
    inherit home;
    git = lib.getExe pkgs.git;
    etc = "${etc}";
    env = import ../../mirrors/env.nix mirrors;
    npmRegistry = mirrors.npm.registry;
    skipPermissions = config.envy.amux.skipPermissions;
    nix = {
      system = linuxSystem;
      tools = toolPaths;
    };
    image = {
      context = "${imageContext}";
      buildArgs = {
        BASE_IMAGE = "${mirrors.docker.registry}/library/debian:trixie-slim";
        APT_MIRROR = aptBootstrap mirrors.apt.debian;
        APT_SECURITY_MIRROR = aptBootstrap mirrors.apt.debianSecurity;
      };
    };
    agents = {
      claude = {
        command = "claude";
        skipArgs = [ "--dangerously-skip-permissions" ];
        args = inheritedArgs config.envy.agents.cc.args
          ++ prompts.argsFor "claude-code";
        envFiles = templatePath "envy-agent-cc" ++ [ (runtimeEnv "cc") ];
        # Claude Code asks about custom keys when ANTHROPIC_API_KEY is set.
        env = [ "ANTHROPIC_BASE_URL" "ANTHROPIC_AUTH_TOKEN" "ANTHROPIC_MODEL" ];
        skills = skillMounts "cc" ".claude/skills";
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
        skills = skillMounts "codex" ".codex/skills";
      };
    };
  });

  launcher = pkgs.writeShellScript "amux" ''
    export AMUX_CONFIG=${settings}
    exec ${python} -I ${./amux.py} "$@"
  '';
  amux = pkgs.runCommand "amux" {
    nativeBuildInputs = [ pkgs.git pkgs.zsh ];
    meta.mainProgram = "amux";
  } ''
    AMUX_PATH=${./amux.py} ${python} ${./test_amux.py}
    zsh -n ${./_amux}
    install -Dm755 ${launcher} $out/bin/amux
    install -Dm644 ${./_amux} $out/share/zsh/site-functions/_amux
  '';
in
{
  # amux runs Claude Code and Codex in detachable Docker sessions. Docker
  # itself (OrbStack on Darwin) is provided outside this module.
  envy.software.nix.packages.include = [ amux ];
  envy.software.nix.packages.references.amux = "local:modules/agents/amux/default.nix#amux";
}
