{ config, lib, ... }:

let
  manifestLib = import ./manifest.nix { inherit lib; };
  unique = values: lib.unique values;
  compatiblePackages = packages:
    lib.all
      (values: builtins.length (lib.unique values) == 1)
      (builtins.attrValues (lib.groupBy lib.getName packages));
  unmatchedReferences = selection:
    lib.subtractLists
      (map lib.getName selection.include)
      (builtins.attrNames selection.references);
  unreferencedPackages = selection:
    lib.subtractLists
      (builtins.attrNames selection.references)
      (map lib.getName selection.include);
  referencesMatchPackages = selection:
    unmatchedReferences selection == [ ] && unreferencedPackages selection == [ ];
  selectPackages = selection: lib.filter
    (package: !(builtins.elem (lib.getName package) selection.exclude))
    (unique selection.include);
  selectStrings = selection: lib.subtractLists selection.exclude (unique selection.include);
  policy = config.envy.darwin;
  softwarePolicy = policy.software;
  mirrorProfile = (import ../mirrors/resolve.nix { inherit lib; })
    (import ../mirrors/catalog.nix).${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  commonMirrors = builtins.removeAttrs mirrorProfile [ "apt" "dockerInstallerMirror" "homebrew" "probes" ];
  systemPackages = selectPackages softwarePolicy.nix.systemPackages;
  fontPackages = selectPackages softwarePolicy.nix.fonts;
  brews = selectStrings softwarePolicy.homebrew.formulae;
  casks = selectStrings softwarePolicy.homebrew.casks;
  taps = selectStrings softwarePolicy.homebrew.repositories;
  managedTaps = lib.filter
    (name: !(builtins.elem name [ "homebrew/core" "homebrew/cask" ]))
    taps;
  homePolicy = config.home-manager.users.${config.envy.user.name}.envy.software;
  homeLlm = config.home-manager.users.${config.envy.user.name}.envy.llm;
  homeAgents = config.home-manager.users.${config.envy.user.name}.envy.agents;
  homeSkills = config.home-manager.users.${config.envy.user.name}.agents.skills.effective;
  systemHabits = config.envy.machine.habits;
  homeHabits = config.home-manager.users.${config.envy.user.name}.envy.machine.habits;
in
{
  imports = [
    ./options.nix
    ./darwin/options.nix
    ../llm/options.nix
    ../agents/options.nix
  ];

  config = {
    assertions = [
      {
        assertion = compatiblePackages softwarePolicy.nix.systemPackages.include;
        message = "envy.darwin.software.nix.systemPackages contains one stable ID with different derivations";
      }
      {
        assertion = referencesMatchPackages softwarePolicy.nix.systemPackages;
        message = "envy.darwin.software.nix.systemPackages.references must match included package names; unknown: ${lib.concatStringsSep ", " (unmatchedReferences softwarePolicy.nix.systemPackages)}; missing: ${lib.concatStringsSep ", " (unreferencedPackages softwarePolicy.nix.systemPackages)}";
      }
      {
        assertion = compatiblePackages softwarePolicy.nix.fonts.include;
        message = "envy.darwin.software.nix.fonts contains one stable ID with different derivations";
      }
      {
        assertion = referencesMatchPackages softwarePolicy.nix.fonts;
        message = "envy.darwin.software.nix.fonts.references must match included package names; unknown: ${lib.concatStringsSep ", " (unmatchedReferences softwarePolicy.nix.fonts)}; missing: ${lib.concatStringsSep ", " (unreferencedPackages softwarePolicy.nix.fonts)}";
      }
    ];
    envy.darwin.software.nix.systemPackages.effective = systemPackages;
    envy.darwin.software.nix.fonts.effective = fontPackages;
    envy.darwin.software.homebrew.formulae.effective = brews;
    envy.darwin.software.homebrew.casks.effective = casks;
    envy.darwin.software.homebrew.repositories.effective = taps;

    envy.machine.manifest = {
      schemaVersion = 3;
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
        "envy.darwin.services.mihomo.mode" = policy.services.mihomo.mode;
        "envy.darwin.services.mihomo.tun" = policy.services.mihomo.tun;
        "envy.darwin.services.openssh.mode" = policy.services.openssh.mode;
        "envy.vscode.mode" = config.envy.vscode.mode;
        "envy.mirrors.mode" = config.envy.mirrors.mode;
      };
      llm = {
        credentials = lib.mapAttrs (_: credential: {
          kind = credential.kind;
          secretPath = credential.secretPath;
        }) homeLlm.credentials;
        endpoints = homeLlm.endpoints;
      };
      agents = lib.mapAttrs (name: agent: agent // {
        configuredSkills = agent.skills;
        skills = homeSkills.${name};
        credentialConfigured = agent.credential != null
          && builtins.hasAttr agent.credential homeLlm.credentials;
        endpointConfigured = agent.endpoint != null
          && builtins.hasAttr agent.endpoint homeLlm.endpoints;
      }) homeAgents;
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
        homebrew = mirrorProfile.homebrew;
        probes = mirrorProfile.probes.common ++ mirrorProfile.probes.darwin;
      };
      habits = systemHabits ++ homeHabits;
      software.sources = config.envy.software.sources;
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
        "nix.system.package" = manifestLib.group {
          label = "Darwin system packages";
          optionPath = "envy.darwin.software.nix.systemPackages";
          ecosystem = "nix";
          platforms = [ "darwin" ];
          scope = "system";
          kind = "package";
          installer = "nix-darwin";
          reconcileUpgrade = true;
          reconcileRemove = true;
          editableInclude = true;
          selection = manifestLib.packageSelection softwarePolicy.nix.systemPackages;
        };
        "nix.system.font" = manifestLib.group {
          label = "Darwin fonts";
          optionPath = "envy.darwin.software.nix.fonts";
          ecosystem = "nix";
          platforms = [ "darwin" ];
          scope = "system";
          kind = "font";
          installer = "nix-darwin";
          reconcileUpgrade = true;
          reconcileRemove = true;
          editableInclude = true;
          selection = manifestLib.packageSelection softwarePolicy.nix.fonts;
        };
        "homebrew.system.formula" = manifestLib.group {
          label = "Homebrew formulae";
          optionPath = "envy.darwin.software.homebrew.formulae";
          ecosystem = "homebrew";
          platforms = [ "darwin" ];
          scope = "system";
          kind = "formula";
          installer = "homebrew";
          editableInclude = true;
          selection = manifestLib.stringSelection "homebrew:formula" softwarePolicy.homebrew.formulae;
        };
        "homebrew.system.cask" = manifestLib.group {
          label = "Homebrew casks";
          optionPath = "envy.darwin.software.homebrew.casks";
          ecosystem = "homebrew";
          platforms = [ "darwin" ];
          scope = "system";
          kind = "cask";
          installer = "homebrew";
          editableInclude = true;
          selection = manifestLib.stringSelection "homebrew:cask" softwarePolicy.homebrew.casks;
        };
        "homebrew.system.repository" = manifestLib.group {
          label = "Homebrew repositories";
          optionPath = "envy.darwin.software.homebrew.repositories";
          ecosystem = "homebrew";
          platforms = [ "darwin" ];
          scope = "system";
          kind = "repository";
          installer = "homebrew";
          editableInclude = true;
          selection = manifestLib.stringSelection "homebrew:tap" softwarePolicy.homebrew.repositories;
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

    environment.systemPackages = systemPackages;
    fonts.packages = fontPackages;
    homebrew = {
      enable = true;
      enableZshIntegration = true;
      inherit brews casks;
      taps = map (name:
        let source = config.envy.software.sources."homebrew:${name}" or null;
        in if source != null && source.parameters ? url then {
          inherit name;
          clone_target = source.parameters.url;
        } else name
      ) managedTaps;
      onActivation = {
        autoUpdate = false;
        upgrade = false;
      };
    };
  };
}
