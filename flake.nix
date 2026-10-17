{
  description = "Chi's cross-platform configuration";

  inputs = {
    # BEGIN ENVY MANAGED INPUTS
    # END ENVY MANAGED INPUTS
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    darwin.url = "github:LnL7/nix-darwin";
    darwin.inputs.nixpkgs.follows = "nixpkgs";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nixgl.url = "github:nix-community/nixGL";
    sops-nix.url = "github:Mic92/sops-nix";
    academic-research-skills = {
      url = "github:Imbad0202/academic-research-skills";
      flake = false;
    };
    academic-research-skills-codex = {
      url = "github:Imbad0202/academic-research-skills-codex";
      flake = false;
    };
    ppt-master = {
      url = "tarball+https://api.github.com/repos/Categorytyy/ppt-master/tarball/master";
      flake = false;
    };
    gpt-image2-ppt-skills = {
      url = "tarball+https://api.github.com/repos/JuneYaooo/gpt-image2-ppt-skills/tarball/main";
      flake = false;
    };
    emil-skills = {
      url = "github:emilkowalski/skills";
      flake = false;
    };
    vercel-agent-skills = {
      url = "github:vercel-labs/agent-skills";
      flake = false;
    };
    waza = {
      url = "github:tw93/waza";
      flake = false;
    };
    mcp-nixos = {
      url = "github:utensils/mcp-nixos";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pi = {
      # Use the GitHub fetcher so flake.lock records an immutable commit rev.
      # The API tarball endpoint for a branch can be re-packed in place and
      # then fail Nix's locked narHash check on every machine.
      url = "github:earendil-works/pi";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, home-manager, darwin, nixgl, sops-nix, ... }@inputs:
    let
      lib = nixpkgs.lib;
      darwinSystem = "aarch64-darwin";
      linuxSystem = "x86_64-linux";
      darwinMachineDir = ./hosts/darwin;
      linuxMachineDir = ./hosts/linux;
      legacyHome = builtins.getEnv "HOME";
      legacyUserConfigPath = legacyHome + "/.config/dotfiles/config.nix";
      legacySystemConfigPath = "/etc/dotfiles/config.nix";
      legacyConfigPath =
        if legacyHome != "" && builtins.pathExists legacyUserConfigPath then legacyUserConfigPath
        else if builtins.pathExists legacySystemConfigPath then legacySystemConfigPath
        else null;
      legacyConfig = if legacyConfigPath == null then null else import legacyConfigPath;
      machineFiles = directory: lib.filterAttrs
        (name: type: type == "regular" && lib.hasSuffix ".nix" name)
        (builtins.readDir directory);
      darwinMachines = machineFiles darwinMachineDir;
      linuxMachines = machineFiles linuxMachineDir;

      softwareSourceBindings = (import ./modules/software/nix-sources.nix) // {
        # BEGIN ENVY MANAGED SOURCE BINDINGS
        # END ENVY MANAGED SOURCE BINDINGS
      };
      softwareSourcePackagesFor = system: lib.mapAttrs (_: binding:
        if binding.input == "nixpkgs" then import nixpkgs { inherit system; config.allowUnfree = true; }
        else lib.attrByPath [ binding.output system ] { } inputs.${binding.input}
      ) softwareSourceBindings;
      softwareSourcePlatforms = lib.mapAttrs (_: binding:
        lib.filter (platform:
          binding.input == "nixpkgs" || lib.hasAttrByPath
            [ binding.output (if platform == "darwin" then darwinSystem else linuxSystem) ]
            inputs.${binding.input}
        ) [ "darwin" "linux" ]
      ) softwareSourceBindings;
      sharedSpecialArgs = machineId: platform: system: {
        inherit machineId;
        softwareSourcePackages = softwareSourcePackagesFor system;
        inherit softwareSourceBindings softwareSourcePlatforms;
        machinePlatform = platform;
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
      };

      mkDarwinConfiguration = fileName: _:
        let
          machineId = lib.removeSuffix ".nix" fileName;
          machineModule = darwinMachineDir + "/${fileName}";
          specialArgs = sharedSpecialArgs machineId "darwin" darwinSystem;
        in darwin.lib.darwinSystem {
          system = darwinSystem;
          inherit specialArgs;
          modules = [
            sops-nix.darwinModules.sops
            ./darwin.nix
            machineModule
            home-manager.darwinModules.home-manager
            ({ config, ... }: {
              home-manager.useGlobalPkgs = true;
              home-manager.useUserPackages = true;
              home-manager.users."${config.envy.user.name}" = import ./home.nix;
              home-manager.backupFileExtension = "backup";
              home-manager.extraSpecialArgs = specialArgs;
              home-manager.sharedModules = [
                sops-nix.homeManagerModules.sops
                machineModule
                {
                  sops.age.keyFile = "${config.envy.user.home}/Library/Application Support/sops/age/keys.txt";
                  sops.age.generateKey = false;
                }
              ];
            })
          ];
        };

      mkLinuxConfigurationFor = machineId: machineModule:
        let
          pkgs = import nixpkgs {
            system = linuxSystem;
            config.allowUnfree = true;
          };
          nixGLPackages = import nixgl {
            inherit pkgs;
            enable32bits = pkgs.stdenv.hostPlatform.isx86;
            enableIntelX86Extensions = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
          };
          # Keep hardware auto-detection on the real Linux host.  When another
          # platform (notably Darwin CI/evaluation) inspects this output, use
          # the pure Mesa wrapper so evaluation never tries to build a Linux
          # /proc-based Nvidia detector on the evaluator.
          evaluatorSystem =
            if builtins ? currentSystem then builtins.currentSystem else null;
          nixGLDefault =
            if evaluatorSystem == linuxSystem
            then nixGLPackages.auto.nixGLDefault
            else nixGLPackages.nixGLIntel;
        in home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          extraSpecialArgs = (sharedSpecialArgs machineId "linux" linuxSystem) // { inherit nixGLDefault; };
          modules = [
            ./home.nix
            machineModule
            sops-nix.homeManagerModules.sops
            ({ config, ... }: {
              sops.age.keyFile = "${config.envy.user.home}/.config/sops/age/keys.txt";
              sops.age.generateKey = false;
            })
          ];
        };

      mkLinuxConfiguration = fileName: _:
        mkLinuxConfigurationFor
          (lib.removeSuffix ".nix" fileName)
          (linuxMachineDir + "/${fileName}");

      legacyLinuxConfiguration =
        if legacyConfig == null then null
        else
          let
            get = path: fallback: lib.attrByPath path fallback legacyConfig;
            user = get [ "home" "user" ] "chi";
            userHome = get [ "home" "dir" ] "/home/${user}";
          in mkLinuxConfigurationFor "default" ({ ... }: {
            imports = [ ./hosts/default.nix ];
            envy.user.name = user;
            envy.user.home = userHome;
            envy.repository.path = get [ "dotfiles" "path" ] "${userHome}/.envy";
            envy.git.name = get [ "git" "name" ] user;
            envy.git.email = get [ "git" "email" ] "${user}@localhost";
            envy.llm.steps.url = get [ "llm" "steps" "url" ] "https://models-proxy.stepfun-inc.com";
            envy.llm.steps.model = get [ "llm" "steps" "model" ] "step-3.7-flash";
            envy.llm.deepseek.url = get [ "llm" "deepseek" "url" ] "https://api.deepseek.com";
            envy.llm.deepseek.model = get [ "llm" "deepseek" "model" ] "deepseek-v4-pro";
            envy.vscode.mode = get [ "vscode" "mode" ] "remote";
            envy.linux.option = get [ "home" "option" ] "desktop";
            envy.linux.desktop = get [ "home" "desktop" ] "all";
          });

      mkDevShell = system: nixpkgs.legacyPackages.${system}.mkShell {
        packages = with nixpkgs.legacyPackages.${system}; [
          jq
          sops
          age
          ssh-to-age
          (python3.withPackages (ps: [
            ps.typer
            ps.rich
            ps.prompt-toolkit
            ps.pyyaml
            ps.tomli-w
          ]))
          git
          curl
          gnupg
          cargo
          rustc
        ] ++ [ inputs.home-manager.packages.${system}.default ];
        shellHook = ''
          export ENVY_DEV_SHELL=1
          export PYTHONPATH="${./resources/scripts}:$PYTHONPATH"
          if [ "''${ENVY_DEBUG:-0}" = "1" ]; then
            echo "[DEBUG] devShell: setup environment ready" >&2
            echo "[DEBUG] Available tools: jq, sops, age, ssh-to-age, python3, typer, rich, prompt_toolkit, cargo, rustc, home-manager" >&2
          fi
        '';
      };

      mkEnvyPython = system:
        nixpkgs.legacyPackages.${system}.python3.withPackages (ps: [
          ps.typer
          ps.rich
          ps.prompt-toolkit
          ps.pyyaml
          ps.tomli-w
        ]);

      mkEnvyRuntime = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = mkEnvyPython system;
        in pkgs.writeShellApplication {
          name = "envy";
          runtimeInputs = [ python ] ++ (with pkgs; [
            gitMinimal
            sops
            age
            ssh-to-age
            curl
          ]);
          text = ''
            export PYTHONPATH="${./resources/scripts}:''${PYTHONPATH:-}"
            exec python3 -m envy "$@"
          '';
        };

      mkEnvySetupRuntime = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = mkEnvyPython system;
        in pkgs.writeShellApplication {
          name = "envy-setup";
          runtimeInputs = [ python ] ++ (with pkgs; [
            gitMinimal
            sops
            age
            ssh-to-age
            curl
          ]);
          text = ''
            export PYTHONPATH="${./resources/scripts}:''${PYTHONPATH:-}"
            exec python3 ${./setup.py} "$@"
          '';
        };

      mkEnvyPythonCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [
            ps.typer
            ps.rich
            ps.prompt-toolkit
            ps.pyyaml
            ps.tomli-w
          ]);
        in pkgs.runCommand "envy-python-tests" {
          nativeBuildInputs = [ python pkgs.git ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          export PYTHONPATH="${./resources/scripts}"
          export ENVY_TEST_ROOT="${./.}"
          cd ${./.}
          python -m compileall -q ${./resources/scripts/envy}
          python -m unittest discover -s ${./resources/scripts/tests} -p 'test_*.py'
          touch "$out"
        '';

      mkEnvyShellCheck = system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in pkgs.runCommand "envy-shell-checks" {
          nativeBuildInputs = [ pkgs.bash pkgs.shellcheck ];
        } ''
          cd ${./.}
          bash -n envy
          bash -n install.sh
          bash -n setup.sh
          bash -n requires.sh
          bash -n resources/scripts/mirror-env.sh
          bash -n ${./resources/scripts/flake-source-mirror.sh}
          bash -n resources/scripts/nix-trust.sh
          shellcheck -x envy install.sh setup.sh requires.sh resources/scripts/mirror-env.sh resources/scripts/flake-source-mirror.sh resources/scripts/nix-trust.sh
          touch "$out"
        '';

      mkSecretSafetyCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
        in pkgs.runCommand "envy-secret-safety" {
          nativeBuildInputs = [ python ];
        } ''
          export PYTHONPATH="${./resources/scripts}"
          python -c 'from pathlib import Path; from envy.sops_format import content_is_sops_encrypted; assert content_is_sops_encrypted(Path("${./secrets/secrets.yaml}").read_text())'
          touch "$out"
        '';

      softwareSourcePackages = lib.genAttrs [ darwinSystem linuxSystem ] softwareSourcePackagesFor;
      darwinConfigurations = lib.mapAttrs' (fileName: value:
        lib.nameValuePair (lib.removeSuffix ".nix" fileName)
          (mkDarwinConfiguration fileName value)) darwinMachines;
      namedHomeConfigurations = lib.mapAttrs' (fileName: value:
        lib.nameValuePair (lib.removeSuffix ".nix" fileName)
          (mkLinuxConfiguration fileName value)) linuxMachines;
      # The historical Linux CLI invoked .#default after pulling. Expose that
      # target only when its ignored legacy config exists, and derive identity
      # from that file instead of guessing one of the versioned hosts.
      # Test only the path here: importing legacy config to decide the output
      # names would also break named targets when an old config link is stale.
      homeConfigurations = namedHomeConfigurations // lib.optionalAttrs
        (legacyConfigPath != null)
        { default = legacyLinuxConfiguration; };

      platformOptionBoundaries =
        lib.all
          (configuration:
            configuration.options.envy ? darwin
            && configuration.options.envy ? environment
            && configuration.options.envy ? shell
            && !(configuration.options.envy ? linux))
          (builtins.attrValues darwinConfigurations)
        && lib.all
          (configuration:
            let
              manual = configuration.extendModules {
                modules = [{ envy.darwin.services.openssh.mode = lib.mkForce "manual"; }];
              };
              disabled = configuration.extendModules {
                modules = [{ envy.darwin.services.openssh.mode = lib.mkForce "none"; }];
              };
              kept = configuration.extendModules {
                modules = [{ envy.darwin.services.openssh.mode = lib.mkForce "keep"; }];
              };
            in
              manual.config.services.openssh.enable == null
              && disabled.config.services.openssh.enable == false
              && kept.config.services.openssh.enable == true)
          (builtins.attrValues darwinConfigurations)
        && lib.all
          (configuration:
            configuration.options.envy ? linux
            && configuration.options.envy ? environment
            && configuration.options.envy ? shell
            && !(configuration.options.envy ? darwin))
          (builtins.attrValues homeConfigurations)
        && lib.all
          (configuration:
            configuration.config.envy.machine.manifest.mirrors ? homebrew
            && !(configuration.config.envy.machine.manifest.mirrors ? apt)
            && !(configuration.config.envy.machine.manifest.mirrors ? dockerInstallerMirror)
            && builtins.elem
              "https://cache.thalheim.io"
              configuration.config.envy.machine.manifest.mirrors.nix.extraSubstituters
            && builtins.elem
              "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
              configuration.config.envy.machine.manifest.mirrors.nix.extraTrustedPublicKeys
            && configuration.config.envy.machine.manifest.schemaVersion == 3
            && configuration.config.envy.machine.manifest.software.sources ? "nix:nixpkgs"
            && configuration.config.envy.machine.manifest ? environment
            && configuration.config.envy.machine.manifest ? shell
            && configuration.config.envy.machine.manifest.software.groups ? "homebrew.system.cask"
            && builtins.any
              (item: item.id == "codegraph")
              configuration.config.envy.machine.manifest.software.groups."npm.user.tool".selection.include
            && builtins.any
              (item: item.id == "headroom")
              configuration.config.envy.machine.manifest.software.groups."pypi.user.tool".selection.include
            && !(configuration.config.envy.machine.manifest.software.groups ? "native.system.package"))
          (builtins.attrValues darwinConfigurations)
        && lib.all
          (configuration:
            configuration.config.envy.machine.manifest.mirrors ? apt
            && configuration.config.envy.machine.manifest.mirrors ? dockerInstallerMirror
            && !(configuration.config.envy.machine.manifest.mirrors ? homebrew)
            && builtins.elem
              "https://cache.thalheim.io"
              configuration.config.envy.machine.manifest.mirrors.nix.extraSubstituters
            && builtins.elem
              "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
              configuration.config.envy.machine.manifest.mirrors.nix.extraTrustedPublicKeys
            && configuration.config.envy.machine.manifest.schemaVersion == 3
            && configuration.config.envy.machine.manifest.software.sources ? "nix:nixpkgs"
            && configuration.config.envy.machine.manifest ? environment
            && configuration.config.envy.machine.manifest ? shell
            && configuration.config.envy.machine.manifest.software.groups ? "native.system.package"
            && !(configuration.config.envy.machine.manifest.software.groups ? "homebrew.system.cask"))
          (builtins.attrValues homeConfigurations);
      mkPlatformOptionCheck = system:
        assert import ./checks/legacy-linux-compatibility.nix { inherit inputs; };
        assert import ./checks/agent-skills.nix { inherit inputs; };
        assert platformOptionBoundaries;
        nixpkgs.legacyPackages.${system}.runCommand "envy-platform-option-boundaries" { } ''
          touch $out
        '';
      linuxPolicyCheck = import ./checks/linux-policy.nix { inherit inputs; };
    in
    {
      inherit darwinConfigurations homeConfigurations softwareSourcePackages softwareSourceBindings;
      legacyPackages = softwareSourcePackages;

      packages.aarch64-darwin.envy = mkEnvyRuntime "aarch64-darwin";
      packages.aarch64-darwin.setup = mkEnvySetupRuntime "aarch64-darwin";
      packages.aarch64-darwin.default = self.packages.aarch64-darwin.envy;
      apps.aarch64-darwin.envy = {
        type = "app";
        program = lib.getExe self.packages.aarch64-darwin.envy;
      };
      apps.aarch64-darwin.setup = {
        type = "app";
        program = lib.getExe self.packages.aarch64-darwin.setup;
      };

      packages.x86_64-linux.envy = mkEnvyRuntime "x86_64-linux";
      packages.x86_64-linux.setup = mkEnvySetupRuntime "x86_64-linux";
      packages.x86_64-linux.default = self.packages.x86_64-linux.envy;
      apps.x86_64-linux.envy = {
        type = "app";
        program = lib.getExe self.packages.x86_64-linux.envy;
      };
      apps.x86_64-linux.setup = {
        type = "app";
        program = lib.getExe self.packages.x86_64-linux.setup;
      };

      devShells.aarch64-darwin.default = mkDevShell "aarch64-darwin";
      devShells.x86_64-linux.default = mkDevShell "x86_64-linux";
      checks.aarch64-darwin.platform-option-boundaries = mkPlatformOptionCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-python-tests = mkEnvyPythonCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-shell-checks = mkEnvyShellCheck "aarch64-darwin";
      checks.aarch64-darwin.secret-safety = mkSecretSafetyCheck "aarch64-darwin";
      checks.x86_64-linux.platform-option-boundaries = mkPlatformOptionCheck "x86_64-linux";
      checks.x86_64-linux.linux-policy-boundaries = linuxPolicyCheck;
      checks.x86_64-linux.envy-python-tests = mkEnvyPythonCheck "x86_64-linux";
      checks.x86_64-linux.envy-shell-checks = mkEnvyShellCheck "x86_64-linux";
      checks.x86_64-linux.secret-safety = mkSecretSafetyCheck "x86_64-linux";
    };
}
