{ inputs }:

let
  system = "x86_64-linux";
  pkgs = import inputs.nixpkgs {
    inherit system;
    config.allowUnfree = true;
  };

  mkConfigurationWith = machineId: option: desktop: exclude: extraPolicy:
    inputs.home-manager.lib.homeManagerConfiguration {
      inherit pkgs;
      extraSpecialArgs = {
        inherit machineId;
        machinePlatform = "linux";
        machineSystem = system;
        academicResearchSkills = inputs.academic-research-skills;
        academicResearchSkillsCodex = inputs.academic-research-skills-codex;
        pptMaster = inputs.ppt-master;
        gptImage2PptSkills = inputs.gpt-image2-ppt-skills;
        emilSkills = inputs.emil-skills;
        vercelAgentSkills = inputs.vercel-agent-skills;
        wazaSource = inputs.waza;
        mcpNixosPackage = inputs.mcp-nixos.packages.${system}.default;
        piSource = inputs.pi.outPath;
        piPackage = inputs.pi.packages.${system}.default;
        softwareSourceBindings = import ../modules/software/nix-sources.nix;
        nixGLDefault = null;
      };
      modules = [
        ../home.nix
        ({ ... }: {
          imports = [ ../hosts/default.nix extraPolicy ];
          envy.user.name = "policy-check";
          envy.user.home = "/home/policy-check";
          envy.repository.path = "/home/policy-check/.envy";
          envy.git.name = "Policy Check";
          envy.git.email = "policy-check@example.com";
          envy.llm.steps.url = "https://example.com";
          envy.llm.steps.model = "model";
          envy.llm.deepseek.url = "https://api.deepseek.com";
          envy.llm.deepseek.model = "model";
          envy.vscode.mode = "remote";
          envy.linux = { inherit option desktop; };
          envy.software.nix.packages = { inherit exclude; };
        })
        inputs.sops-nix.homeManagerModules.sops
        {
          sops.age.keyFile = "/home/policy-check/.config/sops/age/keys.txt";
          sops.age.generateKey = false;
        }
      ];
    };
  mkConfiguration = machineId: option: desktop: exclude:
    mkConfigurationWith machineId option desktop exclude { };
  server = mkConfiguration "policy-server" "server" "all" [ ];
  none = mkConfiguration "policy-none" "desktop" "none" [ ];
  gnome = mkConfiguration "policy-gnome" "desktop" "gnome" [ ];
  niri = mkConfiguration "policy-niri" "desktop" "niri" [ ];
  all = mkConfiguration "policy-all" "desktop" "all" [ ];
  niriExcluded = mkConfiguration "policy-niri-excluded" "desktop" "niri" [ "niri" ];
  sunshineExcluded = mkConfiguration "policy-sunshine-excluded" "desktop" "gnome" [ "sunshine" ];
  vscodeExcluded = mkConfiguration "policy-vscode-excluded" "desktop" "gnome" [ "vscode" ];
  waydroidExcluded = mkConfiguration "policy-waydroid-excluded" "desktop" "gnome" [ "waydroid" ];
  directSelections = mkConfigurationWith
    "policy-direct-selections"
    "server"
    "none"
    [ ]
    ({ pkgs, ... }: {
      envy.environment.sessionVariables = {
        EDITOR = "machine-editor";
        ENVY_POLICY_CHECK = "enabled";
      };
      envy.environment.sessionPath = [ "/opt/envy-policy-check/bin" ];
      envy.shell.zsh.aliases = {
        grep = "rg --hidden";
        policy-check = "envy status";
      };
      envy.shell.zsh.initContent = ''
        export ENVY_ZSH_POLICY_CHECK=enabled
      '';
      envy.software.nix.packages.include = [ pkgs.hello ];
      envy.software.nix.packages.references.hello = "nix:hello";
      envy.linux.software.native.packages.include = [
        {
          id = "curl";
          name = "curl";
          ref = "native:curl";
        }
      ];
    });

  packageNames = configuration: map inputs.nixpkgs.lib.getName configuration.config.home.packages;
  hasPackage = name: configuration: builtins.elem name (packageNames configuration);
  niriScratchpad = builtins.head (builtins.filter
    (package: inputs.nixpkgs.lib.getName package == "niri-scratchpad")
    niri.config.home.packages);
  hasGnome = configuration:
    builtins.hasAttr "org/gnome/desktop/background" configuration.config.dconf.settings;
  hasNiri = configuration:
    builtins.hasAttr "niri/config.kdl" configuration.config.xdg.configFile;
  hasService = name: configuration:
    builtins.hasAttr name configuration.config.systemd.user.services;
  hasActivation = name: configuration:
    builtins.hasAttr name configuration.config.home.activation;

  serverForbiddenPackages = inputs.nixpkgs.lib.intersectLists
    [ "sunshine" "waydroid" "google-chrome" "fcitx5-with-addons" "vscode" "cursor" "niri" "tilix" ]
    (packageNames server);
  serverForbiddenActivations = inputs.nixpkgs.lib.intersectLists
    [ "installWayDroid" "rimeDeploy" "setupSunshineInput" "swayosdSystemSetup" "niriStartUp" ]
    (builtins.attrNames server.config.home.activation);
  sysInitActivation = server.config.home.activation.sysInit.data;
  nixTrustActivation = server.config.home.activation.configureNixDaemonTrust.data;
  nativePackagesActivation = server.config.home.activation.installNativePackages.data;
  sshdActivation = server.config.home.activation.setupSshd.data;
  zshDefaultActivation = server.config.home.activation.setZshAsDefault.data;
in
assert serverForbiddenPackages == [ ];
assert serverForbiddenActivations == [ ];
assert inputs.nixpkgs.lib.hasInfix "esudo_system()" sysInitActivation;
assert inputs.nixpkgs.lib.hasInfix "/bin/env" sysInitActivation;
assert inputs.nixpkgs.lib.hasInfix
  "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  sysInitActivation;
assert hasActivation "configureAptMirror" server;
assert hasActivation "configureNixDaemonTrust" server;
assert inputs.nixpkgs.lib.hasInfix "/bin/envy-nix-trust" nixTrustActivation;
assert hasActivation "installNativePackages" server;
assert !(inputs.nixpkgs.lib.hasInfix "pkg_update || true" nativePackagesActivation);
assert inputs.nixpkgs.lib.hasInfix
  "esudo_system /usr/bin/systemctl enable --now ssh"
  sshdActivation;
assert !(inputs.nixpkgs.lib.hasInfix "esudo /usr/bin/systemctl" sshdActivation);
assert inputs.nixpkgs.lib.hasInfix "/usr/bin/tee" zshDefaultActivation;
assert inputs.nixpkgs.lib.hasInfix "/usr/sbin/usermod" zshDefaultActivation;
assert !(inputs.nixpkgs.lib.hasInfix "esudo chsh" zshDefaultActivation);
assert hasActivation "installNpmTools" server;
assert hasActivation "installPypiTools" server;
assert server.config.envy.machine.manifest.schemaVersion == 3;
assert server.config.envy.machine.manifest.software.groups ? "nix.user.package";
assert server.config.envy.machine.manifest.software.groups ? "native.system.package";
assert server.config.envy.machine.manifest.software.groups ? "npm.user.tool";
assert server.config.envy.machine.manifest.software.groups ? "pypi.user.tool";
assert !(server.config.envy.machine.manifest.software.groups ? "homebrew.system.cask");
assert server.config.envy.machine.manifest.mirrors ? apt;
assert !(server.config.envy.machine.manifest.mirrors ? homebrew);
assert builtins.elem
  "https://cache.thalheim.io"
  server.config.envy.machine.manifest.mirrors.nix.extraSubstituters;
assert builtins.elem
  "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
  server.config.envy.machine.manifest.mirrors.nix.extraTrustedPublicKeys;
assert !(inputs.nixpkgs.lib.hasInfix
  "download-attempts"
  server.config.home.file.".config/nix/nix.conf".text);
assert server.config.home.sessionVariables.UV_DEFAULT_INDEX ==
  "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple";
assert builtins.attrNames server.config.systemd.user.services == [ "sops-nix" ];
assert !hasGnome server && !hasNiri server;
assert !hasGnome none && !hasNiri none;
assert !none.config.programs.gnome-shell.enable;
assert !hasPackage "gnome-shell-extension-sunshinestatus" none;
assert hasGnome gnome && !hasNiri gnome;
assert gnome.config.programs.gnome-shell.enable;
assert hasPackage "quake" gnome;
assert hasPackage "vscode" gnome && gnome.config.programs.vscode.enable;
assert hasPackage "tilix" gnome && !hasPackage "niri" gnome;
assert !hasGnome niri && hasNiri niri;
assert !niri.config.programs.gnome-shell.enable;
assert !hasPackage "gnome-shell-extension-sunshinestatus" niri;
assert hasPackage "niri" niri && hasPackage "niri-scratchpad" niri;
assert hasPackage "screenshot" niri && hasPackage "fix-pipewire" niri;
assert niriScratchpad.meta.mainProgram == "nscratch";
assert !hasPackage "tilix" niri;
assert hasGnome all && hasNiri all;
assert all.config.programs.gnome-shell.enable;
assert !hasPackage "niri" niriExcluded && !hasNiri niriExcluded;
assert !hasPackage "niri-scratchpad" niriExcluded;
assert !hasPackage "screenshot" niriExcluded && !hasPackage "fix-pipewire" niriExcluded;
assert !hasService "swayosd" niriExcluded;
assert !hasActivation "niriStartUp" niriExcluded;
assert !hasActivation "swayosdSystemSetup" niriExcluded;
assert !hasPackage "sunshine" sunshineExcluded;
assert !hasService "sunshine" sunshineExcluded;
assert !hasActivation "setupSunshineInput" sunshineExcluded;
assert !hasPackage "vscode" vscodeExcluded;
assert !vscodeExcluded.config.programs.vscode.enable;
assert !hasActivation "vscodeProfiles" vscodeExcluded;
assert !hasActivation "vscodeRemoteSyncNotice" vscodeExcluded;
assert !hasPackage "waydroid" waydroidExcluded;
assert !hasPackage "waydroid-helper" waydroidExcluded;
assert !hasActivation "installWayDroid" waydroidExcluded;
assert !(builtins.hasAttr "id.waydro.waydroid_helper" waydroidExcluded.config.xdg.desktopEntries);
assert directSelections.config.home.sessionVariables.EDITOR == "machine-editor";
assert directSelections.config.home.sessionVariables.ENVY_POLICY_CHECK == "enabled";
assert directSelections.config.programs.zsh.sessionVariables.EDITOR == "machine-editor";
assert builtins.elem "/opt/envy-policy-check/bin" directSelections.config.home.sessionPath;
assert directSelections.config.programs.zsh.shellAliases.grep == "rg --hidden";
assert directSelections.config.programs.zsh.shellAliases.policy-check == "envy status";
assert inputs.nixpkgs.lib.hasInfix
  "[[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh"
  directSelections.config.programs.zsh.initContent;
assert inputs.nixpkgs.lib.hasSuffix
  "export ENVY_ZSH_POLICY_CHECK=enabled\n"
  directSelections.config.programs.zsh.initContent;
assert directSelections.config.envy.machine.manifest.environment.sessionVariables.EDITOR ==
  "machine-editor";
assert directSelections.config.envy.machine.manifest.environment.sessionPath ==
  [ "/opt/envy-policy-check/bin" ];
assert directSelections.config.envy.machine.manifest.shell.zsh.aliases.policy-check ==
  "envy status";
assert inputs.nixpkgs.lib.hasInfix
  "ENVY_ZSH_POLICY_CHECK"
  directSelections.config.envy.machine.manifest.shell.zsh.initContent;
assert hasPackage "hello" directSelections;
assert builtins.any
  (item: item.id == "curl")
  directSelections.config.envy.machine.manifest.software.groups."native.system.package".selection.effective;
pkgs.runCommand "envy-linux-policy-boundaries" { } ''
  touch $out
''
