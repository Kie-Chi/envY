{ config, lib, ... }:

let
  manifestLib = import ./manifest.nix { inherit lib; };
  policy = config.envy.linux;
  homePolicy = config.envy.software;
  compatibleItems = items:
    lib.all
      (values: builtins.length (lib.unique values) == 1)
      (builtins.attrValues (lib.groupBy (item: item.id) items));
  mirrorProfile = (import ../mirrors/resolve.nix { inherit lib; })
    (import ../mirrors/catalog.nix).${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  commonMirrors = builtins.removeAttrs mirrorProfile [ "apt" "dockerInstallerMirror" "homebrew" "probes" ];
  selectItems = selection: lib.filter
    (item: !(builtins.elem item.id selection.exclude))
    (lib.unique selection.include);
in
{
  imports = [
    ./linux/options.nix
    ../llm/options.nix
    ../agents/options.nix
  ];

  config = {
    assertions = [
      {
        assertion = compatibleItems policy.software.native.packages.include;
        message = "envy.linux.software.native.packages contains the same stable ID with conflicting metadata";
      }
      {
        assertion = compatibleItems policy.software.url.artifacts.include;
        message = "envy.linux.software.url.artifacts contains the same stable ID with conflicting metadata";
      }
    ];
    envy.linux.software.native.packages.effective = selectItems policy.software.native.packages;
    envy.linux.software.url.artifacts.effective = selectItems policy.software.url.artifacts;

    envy.machine.manifest = {
      schemaVersion = 2;
      id = config.envy.machine.id;
      platform = config.envy.machine.platform;
      system = config.envy.machine.system;
      settings = {
        "envy.user.name" = config.envy.user.name;
        "envy.user.home" = config.envy.user.home;
        "envy.repository.path" = config.envy.repository.path;
        "envy.git.name" = config.envy.git.name;
        "envy.git.email" = config.envy.git.email;
        "envy.habits.terminalScratchpad.gesture" = config.envy.habits.terminalScratchpad.gesture;
        "envy.habits.globalLauncher.gesture" = config.envy.habits.globalLauncher.gesture;
        "envy.vscode.mode" = config.envy.vscode.mode;
        "envy.linux.desktop" = policy.desktop;
        "envy.linux.option" = policy.option;
        "envy.mirrors.mode" = config.envy.mirrors.mode;
      };
      llm = {
        credentials = lib.mapAttrs (_: credential: {
          kind = credential.kind;
          secretPath = credential.secretPath;
        }) config.envy.llm.credentials;
        endpoints = config.envy.llm.endpoints;
      };
      agents = lib.mapAttrs (_: agent: agent // {
        credentialConfigured = agent.credential != null
          && builtins.hasAttr agent.credential config.envy.llm.credentials;
        endpointConfigured = agent.endpoint != null
          && builtins.hasAttr agent.endpoint config.envy.llm.endpoints;
      }) config.envy.agents;
      environment = {
        sessionVariables = config.envy.environment.sessionVariables;
        sessionPath = config.envy.environment.sessionPath;
      };
      shell.zsh = {
        aliases = config.envy.shell.zsh.aliases;
        initContent = config.envy.shell.zsh.initContent;
      };
      mirrorOverrides = config.envy.mirrors.overrides;
      mirrors = commonMirrors // {
        mode = config.envy.mirrors.mode;
        apt = mirrorProfile.apt;
        dockerInstallerMirror = mirrorProfile.dockerInstallerMirror;
        probes = mirrorProfile.probes.common ++ mirrorProfile.probes.linux;
      };
      habits = config.envy.machine.habits;
      software.groups = {
        "nix.user.package" = manifestLib.group {
          label = "Nix packages";
          optionPath = "envy.software.nix.packages";
          ecosystem = "nix";
          platforms = [ "darwin" "linux" ];
          scope = "user";
          kind = "package";
          installer = "home-manager";
          reconcileUpgrade = true;
          reconcileRemove = true;
          editableInclude = true;
          selection = manifestLib.packageSelection homePolicy.nix.packages;
        };
        "native.system.package" = manifestLib.group {
          label = "Native system packages";
          optionPath = "envy.linux.software.native.packages";
          ecosystem = "native";
          platforms = [ "linux" ];
          scope = "system";
          kind = "package";
          installer = "native-package-manager";
          editableInclude = true;
          selection = manifestLib.itemSelection policy.software.native.packages;
        };
        "url.system.artifact" = manifestLib.group {
          label = "System artifacts";
          optionPath = "envy.linux.software.url.artifacts";
          ecosystem = "url";
          platforms = [ "linux" ];
          scope = "system";
          kind = "artifact";
          installer = "native-artifact";
          selection = manifestLib.itemSelection policy.software.url.artifacts;
        };
        "npm.user.tool" = manifestLib.group {
          label = "NPM tools";
          optionPath = "envy.software.npm.tools";
          ecosystem = "npm";
          platforms = [ "darwin" "linux" ];
          scope = "user";
          kind = "tool";
          installer = "npm";
          editableInclude = true;
          selection = manifestLib.itemSelection homePolicy.npm.tools;
        };
        "pypi.user.tool" = manifestLib.group {
          label = "Python tools";
          optionPath = "envy.software.pypi.tools";
          ecosystem = "pypi";
          platforms = [ "darwin" "linux" ];
          scope = "user";
          kind = "tool";
          installer = "uv";
          editableInclude = true;
          selection = manifestLib.itemSelection homePolicy.pypi.tools;
        };
      };
    };
  };
}
