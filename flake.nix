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
    anti-defensive-writing = {
      url = "github:Kiterlin/anti-defensive-writing";
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
    prompt-master = {
      url = "github:nidhinjs/prompt-master";
      flake = false;
    };
    # Skill instructions only; the obelisk CLI is pinned separately in
    # modules/agents/tools/obelisk.nix.
    obelisk-skill = {
      url = "github:tommy0103/obelisk-skill";
      flake = false;
    };
    mcp-nixos = {
      url = "github:utensils/mcp-nixos";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-t3code.url = "github:limwa/nix-t3code";
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
      t3codePackages = lib.genAttrs [ darwinSystem linuxSystem ] (system:
        import ./modules/agents/providers/t3code/package.nix {
          inherit lib;
          pkgs = nixpkgs.legacyPackages.${system};
          package = inputs.nix-t3code.packages.${system}.t3code-nightly;
          electron = inputs.nix-t3code.inputs.nixpkgs.legacyPackages.${system}.electron_44;
        });
      sharedSpecialArgs = machineId: platform: system: {
        inherit machineId;
        softwareSourcePackages = softwareSourcePackagesFor system;
        inherit softwareSourceBindings softwareSourcePlatforms;
        machinePlatform = platform;
        machineSystem = system;
        academicResearchSkills = inputs.academic-research-skills;
        academicResearchSkillsCodex = inputs.academic-research-skills-codex;
        antiDefensiveWriting = inputs.anti-defensive-writing;
        pptMaster = inputs.ppt-master;
        gptImage2PptSkills = inputs.gpt-image2-ppt-skills;
        emilSkills = inputs.emil-skills;
        vercelAgentSkills = inputs.vercel-agent-skills;
        wazaSource = inputs.waza;
        promptMaster = inputs.prompt-master;
        obeliskSkill = inputs.obelisk-skill;
        mcpNixosPackage = inputs.mcp-nixos.packages.${system}.default;
        piSource = inputs.pi.outPath;
        piPackage = inputs.pi.packages.${system}.default;
        t3codePackage = t3codePackages.${system};
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
          # Build caches stay outside the checkout: path: flake evaluation
          # hashes every file and bundled sources would capture them.
          export CARGO_TARGET_DIR="''${CARGO_TARGET_DIR:-''${XDG_CACHE_HOME:-$HOME/.cache}/envy/envy-tui-target}"
          export PYTHONPYCACHEPREFIX="''${PYTHONPYCACHEPREFIX:-''${XDG_CACHE_HOME:-$HOME/.cache}/envy/pycache}"
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
          homes = if system == darwinSystem then
            lib.mapAttrs (_: configuration: let
              config = configuration.config;
            in config.home-manager.users.${config.envy.user.name}) darwinConfigurations
          else lib.mapAttrs (_: configuration: configuration.config) homeConfigurations;
          tmuxHomes = lib.filterAttrs (_: config:
            builtins.hasAttr ".tmux.conf.local" config.home.file
          ) homes;
          terminalFixtures = pkgs.writeText "envy-terminal-theme-fixtures.json" (builtins.toJSON {
            selectedCases = lib.mapAttrs (_: config: {
              tmuxSource = config.home.file.".tmux.conf".source;
              tmuxConfig = config.home.file.".tmux.conf.local".text;
            }) tmuxHomes;
            yaziCases = lib.mapAttrs (_: config: {
              binary = lib.getExe pkgs.yazi;
              theme = let file = config.xdg.configFile."yazi/theme.toml";
                in if file.text != null then file.text else builtins.readFile file.source;
            }) (lib.filterAttrs (_: config: config.programs.yazi.enable) homes);
          });
          completionHome = lib.findFirst
            (home: lib.any (package: lib.getName package == "fzf") home.home.packages)
            (throw "Completion tests require a machine selecting fzf") (builtins.attrValues homes);
          shellLines = lib.splitString "\n" completionHome.programs.zsh.initContent;
          styleStart = lib.lists.findFirstIndex
            (lib.hasInfix "zstyle -e ':fzf-tab:*' fzf-flags '") (-1) shellLines;
          styleLines = assert styleStart >= 0; lib.drop styleStart shellLines;
          styleEnd = lib.lists.findFirstIndex
            (line: builtins.match "[[:space:]]*'[[:space:]]*" line != null) (-1) (builtins.tail styleLines);
          completionFixtures = pkgs.writeText "envy-completion-fixtures.json" (builtins.toJSON {
            zsh = lib.getExe pkgs.zsh;
            fzf = lib.getExe pkgs.fzf;
            zoxide = lib.getExe pkgs.zoxide;
            ohMyZsh = "${pkgs.oh-my-zsh}/share/oh-my-zsh";
            suggestions = "${pkgs.zsh-autosuggestions}/share/zsh-autosuggestions/zsh-autosuggestions.zsh";
            syntax = "${pkgs.zsh-syntax-highlighting}/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh";
            fzfTab = "${pkgs.zsh-fzf-tab}/share/fzf-tab/fzf-tab.plugin.zsh";
            systemctlCompletion = "${pkgs.callPackage ./modules/cores/packages/systemd-completions.nix { }}/share/zsh/site-functions";
            omzPlugins = completionHome.programs.zsh.oh-my-zsh.plugins;
            # Limit this fixture to completion integrations, excluding prompt and history UIs.
            initLines = builtins.filter (line: lib.any (text: lib.hasInfix text line) [
              "zsh-autosuggestions.zsh" "oh-my-zsh.sh" "/bin/zoxide init zsh"
              "/bin/fzf --zsh" "/share/fzf-tab/fzf-tab.plugin.zsh" "zsh-syntax-highlighting.zsh"
            ]) shellLines;
            fzfOptions = completionHome.home.sessionVariables.FZF_DEFAULT_OPTS;
            fzfFlags = assert styleEnd >= 0;
              lib.concatStringsSep "\n" (lib.take (styleEnd + 2) styleLines);
          });
          python = pkgs.python3.withPackages (ps: [
            ps.typer
            ps.rich
            ps.prompt-toolkit
            ps.pyyaml
            ps.tomli-w
          ]);
        in pkgs.runCommand "envy-python-tests" {
          passthru.completionFixtures = completionFixtures;
          passthru.terminalFixtures = terminalFixtures;
          nativeBuildInputs = [ python pkgs.git pkgs.tmux pkgs.neovim-unwrapped pkgs.perl pkgs.zsh pkgs.fzf pkgs.zoxide ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          export PYTHONPATH="${./resources/scripts}"
          export ENVY_TEST_ROOT="${./.}"
          export ENVY_TERMINAL_THEME_FIXTURES=${terminalFixtures}
          export ENVY_COMPLETION_FIXTURES=${completionFixtures}
          export ENVY_REQUIRE_TMUX_TESTS=1
          cd ${./.}
          python -m compileall -q ${./resources/scripts/envy}
          python -m unittest discover -s ${./resources/scripts/tests} -p 'test_*.py'
          touch "$out"
        '';

      mkEnvyStorageCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = mkEnvyPython system;
          organize = pkgs.callPackage ./modules/storage/organize.nix { };
          ruleFixture = pkgs.writeText "envy-storage-update-rules.json" (builtins.toJSON {
            home = "/envy-storage-fixture-home";
            rules = (import ./modules/storage/rules {
              home = "/envy-storage-fixture-home";
              machinePlatform = "darwin";
            }).rules;
          });
        in pkgs.runCommand "envy-storage-tests" {
          nativeBuildInputs = [ python organize pkgs.restic pkgs.lsof pkgs.git ];
        } ''
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          export PYTHONPATH="${./resources/scripts}"
          export ENVY_ROOT="${./.}"
          export ENVY_STORAGE_RULES_FIXTURE="${ruleFixture}"
          python -m unittest discover -s ${./resources/scripts/tests} -p 'test_storage*.py'
          touch "$out"
        '';

      mkEnvyTuiCheck = system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in pkgs.callPackage ./modules/cores/packages/envy-tui.nix {};

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
          bash -n modules/desktops/darwin/check-app-management.sh
          shellcheck -x envy install.sh setup.sh requires.sh \
            resources/scripts/mirror-env.sh resources/scripts/flake-source-mirror.sh \
            resources/scripts/nix-trust.sh resources/scripts/nix-darwin-etc.sh \
            resources/scripts/spk resources/scripts/ppack resources/scripts/scrctl \
            resources/helpers/fix-pipewire resources/helpers/nscrctl \
            resources/helpers/quake resources/helpers/screenshot \
            modules/agents/amux/image/entry.sh \
            modules/desktops/darwin/check-app-management.sh
          # Sourced fragments carry no shebang; name the shell that reads them.
          shellcheck -s sh modules/agents/amux/image/profile.sh
          shellcheck -s bash files/fcitx/deploy.sh
          touch "$out"
        '';

      mkEnvyLintCheck = system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in pkgs.runCommand "envy-lint" {
          nativeBuildInputs = [ pkgs.python3Packages.pyflakes pkgs.deadnix ];
        } ''
          cd ${./.}
          pyflakes setup.py resources/scripts/envy resources/scripts/tests resources/helpers modules/agents
          deadnix --fail .
          touch "$out"
        '';

      # Parity between the Nix Codex provider and `envy agent` runtime writes.
      mkEnvyCodexCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [
            ps.typer ps.rich ps.prompt-toolkit ps.pyyaml ps.tomli-w
          ]);
          fixtures = pkgs.writeText "codex-config-fixtures.json" (builtins.toJSON
            (import ./modules/agents/providers/codex/tests/fixtures.nix { inherit lib pkgs; }));
        in pkgs.runCommand "codex-config-tests" {
          nativeBuildInputs = [ python ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          export PYTHONPATH="${./resources/scripts}"
          python ${./resources/scripts/tests/test_agent_codex.py} ${fixtures}
          touch "$out"
        '';

      mkEnvyPiModelCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          python = pkgs.python3.withPackages (ps: [
            ps.typer ps.rich ps.prompt-toolkit ps.pyyaml ps.tomli-w
          ]);
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          fixtures = pkgs.writeText "pi-model-fixtures.json" (builtins.toJSON
            (import ./modules/agents/providers/pi/tests/models.nix { inherit lib pkgs piPackage; }));
          extensions = import ./modules/agents/providers/pi/extensions.nix {
            inherit lib pkgs piPackage;
            config.envy.software.nix.packages.exclude = [ ];
            piSource = inputs.pi.outPath;
          };
          btwPackage = extensions.home.file.".pi/agent/extensions/envy-btw".source;
        in pkgs.runCommand "pi-model-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 python (lib.getBin pkgs.nix) ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          export PYTHONPATH="${./resources/scripts}"
          python ${./modules/agents/providers/pi/tests/pi-models.py} ${fixtures}
          python ${./modules/agents/providers/pi/tests/pi-model-diagnostics.py} \
            ${./modules/agents/providers/pi/models.nix} ${nixpkgs.outPath}/lib
          node ${./modules/agents/providers/pi/tests/pi-models.mjs} \
            ${patchedPi}/lib/pi/node_modules ${fixtures} ${btwPackage}
          touch "$out"
        '';

      mkEnvyPiPowerlineCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          extension = import ./modules/agents/providers/pi/powerline.nix { inherit lib pkgs; };
        in pkgs.runCommand "pi-powerline-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
        } ''
          node ${./modules/agents/providers/pi/tests/pi-powerline.mjs} \
            ${extension} ${patchedPi}/lib/pi/node_modules
          touch "$out"
        '';

      mkEnvyPiAutoAssignedCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          extension = import ./modules/agents/providers/pi/auto-assigned.nix {
            inherit pkgs;
            piSource = inputs.pi.outPath;
          };
        in pkgs.runCommand "pi-auto-assigned-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
        } ''
          node ${./modules/agents/providers/pi/tests/pi-auto-assigned.mjs} \
            ${extension} ${patchedPi}/lib/pi/node_modules
          touch "$out"
        '';

      mkEnvyPiQueueCheck = system:
        import ./modules/agents/providers/pi/queue.nix {
          inherit lib;
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
        };

      mkEnvySourceCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [
            ps.typer ps.rich ps.prompt-toolkit ps.pyyaml ps.tomli-w
          ]);
        in pkgs.runCommand "envy-source-tests" {
          passthru.bootProbe = import ./checks/runtime/boot.nix { inherit pkgs; };
          passthru.installProbe = import ./checks/runtime/install.nix { inherit pkgs; };
          passthru.fullProbe = import ./checks/runtime/full.nix { inherit pkgs; };
          nativeBuildInputs = [ python pkgs.git (lib.getBin pkgs.nix) ]
            ++ lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.ps ];
        } ''
          export HOME="$TMPDIR/home"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          mkdir -p "$HOME"
          ${lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
            export ENVY_RUNTIME_TEST_DARWIN_PS=${pkgs.ps}/bin/ps
            export ENVY_RUNTIME_DARWIN_PS=${pkgs.ps}/bin/ps
          ''}
          python -m unittest discover -s ${./.}/checks/runtime -p 'test_*.py'
          python ${./.}/checks/runtime/source_visibility.py \
            --scripts ${./.}/resources/scripts \
            --nixpkgs-lib ${nixpkgs}/lib \
            --nix ${lib.getBin pkgs.nix}/bin/nix \
            --git ${pkgs.git}/bin/git > "$out"
        '';

      mkGithubActionsCheck = system:
        import ./checks/github-actions.nix {
          pkgs = nixpkgs.legacyPackages.${system};
        };

      mkEnvyPiGoalCheck = system:
        import ./modules/agents/providers/pi/goal.nix {
          inherit lib;
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
        };

      mkEnvyPiSubagentCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          subagents = import ./modules/agents/providers/pi/subagents.nix { inherit lib pkgs; };
          package = subagents.home.file.".pi/agent/extensions/envy-subagents".source;
        in pkgs.runCommand "pi-subagent-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          node ${./modules/agents/providers/pi/tests/pi-subagents.mjs} \
            ${package} ${piPackage}/lib/pi/node_modules ${./files/agents/subagents}
          touch "$out"
        '';

      mkEnvyPiLspCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          lsp = import ./modules/agents/providers/pi/lsp.nix {
            inherit lib pkgs piPackage;
          };
        in pkgs.runCommand "pi-lsp-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
        } ''
          node ${./modules/agents/providers/pi/tests/pi-lsp.mjs} \
            ${lsp} ${patchedPi}/lib/pi/node_modules \
            ${lib.escapeShellArg (builtins.toJSON lsp.serverCommand)}
          touch "$out"
        '';

      mkEnvyPiContextCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          views = import ./modules/agents/providers/pi/views.nix { inherit lib pkgs; };
          tui = pkgs.writeShellApplication {
            name = "pi-context-tui-tests";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              exec python3 ${./modules/agents/providers/pi/tests/pi-context-tui.py} \
                ${pkgs.nodejs_24}/bin/node \
                ${patchedPi}/lib/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
                ${views.context}
            '';
          };
        in pkgs.runCommand "pi-context-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
          passthru = { inherit tui; };
        } ''
          node ${./modules/agents/providers/pi/tests/pi-context.mjs} \
            ${views.context} ${patchedPi}/lib/pi/node_modules
          touch "$out"
        '';

      mkEnvyPiDiffCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          views = import ./modules/agents/providers/pi/views.nix { inherit lib pkgs; };
          tui = pkgs.writeShellApplication {
            name = "pi-diff-tui-tests";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              python3 ${./modules/agents/providers/pi/tests/pi-context-tui.py} \
                ${pkgs.nodejs_24}/bin/node \
                ${patchedPi}/lib/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
                ${views.context} ${views.diff} ${pkgs.git}/bin/git
              exec python3 ${./modules/agents/providers/pi/tests/pi-diff-latency.py} \
                ${pkgs.nodejs_24}/bin/node \
                ${patchedPi}/lib/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
                ${views.diff} ${pkgs.git}/bin/git
            '';
          };
        in pkgs.runCommand "pi-diff-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 pkgs.git ];
          passthru = { inherit tui; };
        } ''
          node ${./modules/agents/providers/pi/tests/pi-diff.mjs} \
            ${views.diff} ${patchedPi}/lib/pi/node_modules
          node ${./modules/agents/providers/pi/tests/pi-diff-scans.mjs} \
            ${views.diff} ${patchedPi}/lib/pi/node_modules
          touch "$out"
        '';

      mkEnvyPiCoexistCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          extensions = import ./modules/agents/providers/pi/extensions.nix {
            inherit lib pkgs piPackage;
            config.envy.software.nix.packages.exclude = [ ];
            piSource = inputs.pi.outPath;
          };
          subagents = import ./modules/agents/providers/pi/subagents.nix { inherit lib pkgs; };
          files = extensions.home.file // subagents.home.file;
          sources = lib.mapAttrsToList (_: file: toString file.source)
            (lib.filterAttrs (target: _: lib.hasPrefix ".pi/agent/extensions/" target) files);
          sourceJson = lib.escapeShellArg (builtins.toJSON sources);
          keybindings = (lib.findFirst (file: file.target == ".pi/agent/keybindings.json")
            (throw "Pi navigation tests require managed keybindings") extensions.envy.agents.pi.runtimeFiles).data;
          bindingsJson = lib.escapeShellArg (builtins.toJSON keybindings);
          views = import ./modules/agents/providers/pi/views.nix { inherit lib pkgs; };
          lsp = import ./modules/agents/providers/pi/lsp.nix { inherit lib pkgs; };
          tui = pkgs.writeShellApplication {
            name = "pi-coexist-tui-tests";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              exec python3 ${./modules/agents/providers/pi/tests/pi-context-tui.py} \
                ${pkgs.nodejs_24}/bin/node \
                ${patchedPi}/lib/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
                ${views.context} ${views.diff} ${pkgs.git}/bin/git ${sourceJson} ${bindingsJson}
            '';
          };
        in pkgs.runCommand "pi-coexist-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 pkgs.git ];
          passthru = { inherit tui; };
        } ''
          node ${./modules/agents/providers/pi/tests/pi-coexist.mjs} \
            ${patchedPi}/lib/pi/node_modules ${sourceJson} \
            ${builtins.head lsp.serverCommand} ${./files/agents/subagents}
          node ${./modules/agents/providers/pi/tests/pi-navigation.mjs} \
            ${patchedPi}/lib/pi/node_modules \
            ${extensions.home.file.".pi/agent/extensions/envy-btw".source} \
            ${extensions.home.file.".pi/agent/extensions/envy-goal-x".source} ${bindingsJson}
          touch "$out"
        '';

      mkEnvyPiDoctorCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          piPackage = inputs.pi.packages.${system}.default;
          patchedPi = import ./modules/agents/providers/pi/package.nix {
            inherit lib piPackage;
            moreThinking = true;
          };
          extensions = import ./modules/agents/providers/pi/extensions.nix {
            inherit lib pkgs piPackage;
            piSource = inputs.pi.outPath;
            config.envy.software.nix.packages.exclude = [ ];
          };
        in pkgs.runCommand "pi-doctor-tests" {
          nativeBuildInputs = [ pkgs.nodejs_24 ];
        } ''
          node ${./modules/agents/providers/pi/tests/pi-doctor.mjs} \
            ${./files/agents/pi/doctor.mjs} ${patchedPi}/lib/pi/node_modules
          node ${./modules/agents/providers/pi/tests/pi-cmux.mjs} \
            ${patchedPi}/lib/pi/node_modules ${./files/agents/pi/cmux.ts} \
            ${extensions.home.file.".pi/agent/extensions/envy-notify.ts".source}
          touch "$out"
        '';

      # Agent tools also run these tests while building their packages, but
      # CI evaluates with --no-build, so expose them as standalone checks.
      mkEnvyAgentCheck = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [ ps.readability-lxml ps.html2text ]);
        in pkgs.runCommand "envy-agent-tests" {
          nativeBuildInputs = [ python pkgs.git pkgs.zsh pkgs.nodejs_24 ];
        } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          AMUX_PATH=${./modules/agents/amux/amux.py} AMUX_COMPLETION=${./modules/agents/amux/_amux} \
            FETCH_AGENTS_PATH=${./modules/agents/amux/image/fetch-agents.py} python ${./modules/agents/amux/test_amux.py}
          zsh -n ${./modules/agents/amux/_amux}
          ENVY_READER_PATH=${./modules/agents/skills/read/scripts/read.py} python ${./modules/agents/tests/test_reader.py}
          ENVY_DEEPSEEK_SEARCH_PATH=${./modules/agents/tools/deepseek_search.py} python ${./modules/agents/tests/test_deepseek_search.py}
          ${import ./modules/agents/tests/wrappers.nix { inherit lib pkgs; }}
          ${import ./modules/agents/tests/runtime-files.nix { inherit lib pkgs; }}
          node --disable-warning=ExperimentalWarning ${./modules/agents/tests/cmux-setup.mjs} \
            ${./files/agents/cmux/setup.mjs}
          touch "$out"
        '';

      # Decrypted secrets and rendered templates must stay private to their
      # owner on every machine; list the ones that grant group or other access.
      exposedSecretFiles =
        let
          sopsConfigs =
            lib.concatMap
              (configuration:
                [ configuration.config.sops ]
                ++ map (home: home.sops) (builtins.attrValues configuration.config.home-manager.users))
              (builtins.attrValues darwinConfigurations)
            ++ map (configuration: configuration.config.sops) (builtins.attrValues homeConfigurations);
          exposed = sops: kind:
            lib.mapAttrsToList (name: file: "${kind}.${name} (${file.mode})")
              (lib.filterAttrs (_: file: builtins.match "0?[0-7]00" file.mode == null) (sops.${kind} or { }));
        in lib.unique (lib.concatMap (sops: exposed sops "secrets" ++ exposed sops "templates") sopsConfigs);

      mkSecretSafetyCheck = system:
        assert lib.assertMsg (exposedSecretFiles == [ ])
          "sops files readable beyond their owner: ${lib.concatStringsSep ", " exposedSecretFiles}";
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
      homeConfigurations = lib.mapAttrs' (fileName: value:
        lib.nameValuePair (lib.removeSuffix ".nix" fileName)
          (mkLinuxConfiguration fileName value)) linuxMachines;

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
              users = configuration.config.home-manager.users;
              sysInit = users.${configuration.config.envy.user.name}.home.activation.sysInit.data;
            in
              # Native package helpers are Linux-only activation functions.
              lib.hasInfix "esudo()" sysInit && !(lib.hasInfix "esudo_system" sysInit))
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
          (builtins.attrValues homeConfigurations)
        && lib.all
          (home:
            # Agents, Raycast, and Zotero read LLM keys only through logical
            # credentials; scalar-era names such as llm-steps-apikey are retired.
            lib.all
              (name: !(lib.hasPrefix "llm-" name) || lib.hasPrefix "llm-credential-" name)
              (builtins.attrNames home.sops.secrets))
          (map
            (configuration: configuration.config.home-manager.users.${configuration.config.envy.user.name})
            (builtins.attrValues darwinConfigurations)
          ++ map (configuration: configuration.config) (builtins.attrValues homeConfigurations));
      mkPlatformOptionCheck = system:
        assert import ./checks/agent-skills.nix { inherit inputs; };
        assert import ./checks/agent-tools.nix { inherit lib; };
        assert import ./checks/habit-readback.nix { inherit inputs; };
        assert platformOptionBoundaries;
        assert import ./checks/homebrew-trust.nix { inherit lib darwinConfigurations; };
        assert import ./checks/terminal-themes.nix { inherit lib darwinConfigurations homeConfigurations; };
        nixpkgs.legacyPackages.${system}.runCommand "envy-platform-option-boundaries" { } ''
          touch $out
        '';
      mkT3CodeSettingsCheck = system:
        import ./checks/t3code-settings.nix {
          inherit lib darwinConfigurations homeConfigurations;
          pkgs = nixpkgs.legacyPackages.${system};
        };
      mkStorageRulesCheck = system:
        assert import ./checks/storage-rules.nix { inherit lib; };
        nixpkgs.legacyPackages.${system}.runCommand "envy-storage-rule-selection" { } ''
          touch $out
        '';
      linuxPolicyCheck = import ./checks/linux-policy.nix { inherit inputs; };
    in
    {
      inherit darwinConfigurations homeConfigurations softwareSourcePackages softwareSourceBindings;
      legacyPackages = softwareSourcePackages;

      packages.aarch64-darwin.t3code-nightly = t3codePackages.aarch64-darwin;
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

      packages.x86_64-linux.t3code-nightly = t3codePackages.x86_64-linux;
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
      checks.aarch64-darwin.t3code-settings = mkT3CodeSettingsCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-python-tests = mkEnvyPythonCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-storage-tests = mkEnvyStorageCheck "aarch64-darwin";
      checks.aarch64-darwin.storage-rule-selection = mkStorageRulesCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-tui-tests = mkEnvyTuiCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-shell-checks = mkEnvyShellCheck "aarch64-darwin";
      checks.aarch64-darwin.secret-safety = mkSecretSafetyCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-lint = mkEnvyLintCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-github-actions = mkGithubActionsCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-source-tests = mkEnvySourceCheck "aarch64-darwin";
      checks.aarch64-darwin.envy-agent-tests = mkEnvyAgentCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-powerline-tests = mkEnvyPiPowerlineCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-auto-assigned-tests = mkEnvyPiAutoAssignedCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-queue-tests = mkEnvyPiQueueCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-goal-tests = mkEnvyPiGoalCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-model-tests = mkEnvyPiModelCheck "aarch64-darwin";
      checks.aarch64-darwin.codex-config-tests = mkEnvyCodexCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-subagent-tests = mkEnvyPiSubagentCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-doctor-tests = mkEnvyPiDoctorCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-lsp-tests = mkEnvyPiLspCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-context-tests = mkEnvyPiContextCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-diff-tests = mkEnvyPiDiffCheck "aarch64-darwin";
      checks.aarch64-darwin.pi-coexist-tests = mkEnvyPiCoexistCheck "aarch64-darwin";
      checks.x86_64-linux.platform-option-boundaries = mkPlatformOptionCheck "x86_64-linux";
      checks.x86_64-linux.t3code-settings = mkT3CodeSettingsCheck "x86_64-linux";
      checks.x86_64-linux.linux-policy-boundaries = linuxPolicyCheck;
      checks.x86_64-linux.envy-python-tests = mkEnvyPythonCheck "x86_64-linux";
      checks.x86_64-linux.envy-storage-tests = mkEnvyStorageCheck "x86_64-linux";
      checks.x86_64-linux.storage-rule-selection = mkStorageRulesCheck "x86_64-linux";
      checks.x86_64-linux.envy-tui-tests = mkEnvyTuiCheck "x86_64-linux";
      checks.x86_64-linux.envy-shell-checks = mkEnvyShellCheck "x86_64-linux";
      checks.x86_64-linux.secret-safety = mkSecretSafetyCheck "x86_64-linux";
      checks.x86_64-linux.envy-lint = mkEnvyLintCheck "x86_64-linux";
      checks.x86_64-linux.envy-github-actions = mkGithubActionsCheck "x86_64-linux";
      checks.x86_64-linux.envy-source-tests = mkEnvySourceCheck "x86_64-linux";
      checks.x86_64-linux.envy-agent-tests = mkEnvyAgentCheck "x86_64-linux";
      checks.x86_64-linux.pi-powerline-tests = mkEnvyPiPowerlineCheck "x86_64-linux";
      checks.x86_64-linux.pi-auto-assigned-tests = mkEnvyPiAutoAssignedCheck "x86_64-linux";
      checks.x86_64-linux.pi-queue-tests = mkEnvyPiQueueCheck "x86_64-linux";
      checks.x86_64-linux.pi-goal-tests = mkEnvyPiGoalCheck "x86_64-linux";
      checks.x86_64-linux.pi-model-tests = mkEnvyPiModelCheck "x86_64-linux";
      checks.x86_64-linux.codex-config-tests = mkEnvyCodexCheck "x86_64-linux";
      checks.x86_64-linux.pi-subagent-tests = mkEnvyPiSubagentCheck "x86_64-linux";
      checks.x86_64-linux.pi-doctor-tests = mkEnvyPiDoctorCheck "x86_64-linux";
      checks.x86_64-linux.pi-lsp-tests = mkEnvyPiLspCheck "x86_64-linux";
      checks.x86_64-linux.pi-context-tests = mkEnvyPiContextCheck "x86_64-linux";
      checks.x86_64-linux.pi-diff-tests = mkEnvyPiDiffCheck "x86_64-linux";
      checks.x86_64-linux.pi-coexist-tests = mkEnvyPiCoexistCheck "x86_64-linux";
    };
}
