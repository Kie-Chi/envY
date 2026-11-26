{ config, lib, machinePlatform, pkgs, sys, t3codePackage, ... }@args:

let
  casks = if machinePlatform == "darwin"
    then args.osConfig.envy.darwin.software.homebrew.casks.effective
    else [];
  selected = builtins.elem (lib.getName t3codePackage) packages;
  packages = map lib.getName config.envy.software.nix.packages.effective;
  profileBin = "${config.home.profileDirectory}/bin";
  service = config.envy.t3code.service;
  serviceEnv = "${config.home.homeDirectory}/.t3/service.env";
  serviceDeclaration = pkgs.writeText "t3code-service-env.json" (builtins.toJSON {
    T3CODE_HOST = service.host;
    T3CODE_PORT = toString service.port;
    T3CODE_LAN = lib.boolToString service.lan;
  });
  serviceNode = lib.getExe pkgs.nodejs_22;
  serviceRunner = pkgs.writeShellScript "envy-t3code-service" ''
    set -eu
    umask 077
    ${serviceNode} ${./service.mjs} merge ${lib.escapeShellArg serviceEnv} ${serviceDeclaration}
    exec ${serviceNode} ${./service.mjs} run ${lib.escapeShellArg serviceEnv} ${t3codePackage}/bin/t3
  '';
  serviceLogs = "${config.home.homeDirectory}/.local/state/envy/t3code/logs";
  palette = import ../../../cores/tokyo-night.nix;
  prompts = import ../../prompts.nix { inherit lib pkgs; };
  # Home Manager normalizes repeatable Ghostty settings to lists.
  settingValue = value: if builtins.isList value then builtins.head value else value;
  font = settingValue (config.programs.ghostty.settings.font-family or "Maple Mono NF CN");
  fontSize = settingValue (config.programs.ghostty.settings.font-size or 13);
  codexSelected = builtins.elem "codex" (packages ++ casks);
  codexBinary = if builtins.elem "codex" packages
    then "${profileBin}/codex"
    else "${args.osConfig.homebrew.prefix}/bin/codex";
  selections = import ../codex/models.nix { inherit lib; } {
    agent = config.envy.agents.codex;
    profiles = config.envy.agentProfiles;
    inherit (config.envy.llm) endpoints credentials;
  };
  defaultSelection = {
    instanceId = "codex";
    model = selections.default.selection.model;
  };
  instances = lib.optionalAttrs codexSelected {
    codex = {
      driver = "codex";
      displayName = "Codex (envY)";
      enabled = true;
      config = {
        setupMode = "existing";
        binaryPath = codexBinary;
        launchArgs = lib.escapeShellArgs (prompts.argsFor "codex");
        customModels = map (entry: entry.selection.model) selections.bindings;
      };
    };
  } // lib.optionalAttrs (builtins.elem "claude" packages) {
    claudeAgent = {
      driver = "claudeAgent";
      displayName = "Claude Code (envY)";
      enabled = true;
      config = {
        binaryPath = "${profileBin}/claude";
        customModels = lib.optional (config.envy.agents.cc.model != null) config.envy.agents.cc.model;
      };
    };
  } // lib.optionalAttrs (builtins.elem "pi" packages) {
    pi = {
      driver = "pi";
      displayName = "Pi (envY)";
      enabled = true;
      config.binaryPath = "${profileBin}/pi";
    };
  };
  theme = {
    name = "envY Tokyo Night";
    appearance = "dark";
    canvas = palette.background;
    accent = builtins.elemAt palette.ansi 4;
    colors = {
      text = palette.foreground;
      textMuted = builtins.elemAt palette.ansi 7;
      codeBackground = palette.fzf.bg;
      codeForeground = palette.foreground;
      error = builtins.elemAt palette.ansi 1;
      terminalBackground = palette.background;
      terminalForeground = palette.foreground;
      terminalCursor = palette.foreground;
      terminalSelection = palette.fzf."bg+";
    };
  };
  files = [
    {
      name = "t3code-theme";
      target = ".t3/userdata/themes/envy-tokyo-night.json";
      data = theme;
    }
    {
      name = "t3code-server";
      target = ".t3/userdata/settings.json";
      data = {
        providerInstances = instances;
        defaultTheme = "envy-tokyo-night";
        defaultThemeSetAt = builtins.hashString "sha256" (builtins.toJSON theme);
        defaultAppearance = "dark";
        defaultAppearanceSetAt = "envy-dark-v1";
      } // lib.optionalAttrs codexSelected {
        defaultModelSelection = defaultSelection;
        textGenerationModelSelection = defaultSelection;
      };
    }
    {
      name = "t3code-desktop";
      target = ".t3/userdata/desktop-settings.json";
      data.localEnvironmentEnabled = false;
    }
    {
      name = "t3code-client";
      target = ".t3/userdata/client-settings.json";
      data = {
        fontFamilyCode = font;
        fontFamilyTerminal = font;
        fontSizeCode = fontSize;
        fontSizeTerminal = fontSize;
      };
    }
  ];
in
{
  imports = [ (../../. + "/${machinePlatform}/t3code-service.nix") ];
  config = lib.mkMerge [
    {
      _module.args.t3codeService = {
        inherit selected;
        runner = serviceRunner;
        logs = serviceLogs;
        environment = {
          HOME = config.home.homeDirectory;
          T3CODE_HOME = "${config.home.homeDirectory}/.t3";
          T3CODE_NIX_PACKAGE = toString t3codePackage;
          T3CODE_NIX_VERSION = t3codePackage.version;
          PATH = lib.concatStringsSep ":" (map
            (lib.replaceStrings [ "$HOME" ] [ config.home.homeDirectory ])
            config.home.sessionPath ++ [ profileBin "${config.home.homeDirectory}/.local/bin"
              "/run/current-system/sw/bin" "/nix/var/nix/profiles/default/bin"
              "/usr/local/bin" "/usr/bin" "/bin" "/usr/sbin" "/sbin" ]
            ++ lib.optional (machinePlatform == "darwin") "${args.osConfig.homebrew.prefix}/bin");
        };
      };
      envy.software.nix.packages = {
        include = [ t3codePackage ];
        references.${lib.getName t3codePackage} = "local:modules/agents/providers/t3code/package.nix";
        sourceDependencies.${lib.getName t3codePackage} = [ "flake:nix-t3code#t3code-nightly" ];
      };
    }
    (lib.mkIf selected {
      home.file = {
        ".local/state/envy/t3code/package" = { source = t3codePackage; force = true; };
        ".local/bin/t3" = { source = "${t3codePackage}/bin/t3"; force = true; };
        ".local/state/envy/t3code/service".source = serviceRunner;
      };
      home.activation = { prepareT3CodeService = lib.hm.dag.entryBetween
        (if machinePlatform == "darwin" then [ "setupLaunchAgents" ] else [ "reloadSystemd" ])
        [ "linkGeneration" "mergeT3Code-t3code-desktop" ] ''
          if [ -z "$DRY_RUN_CMD" ]; then
            (
              umask 077
              mkdir -p ${lib.escapeShellArg serviceLogs}
              touch ${lib.escapeShellArg serviceLogs}/service.log ${lib.escapeShellArg serviceLogs}/service.err.log
              chmod 600 ${lib.escapeShellArg serviceLogs}/service.log ${lib.escapeShellArg serviceLogs}/service.err.log
              ${serviceNode} ${./service.mjs} merge ${lib.escapeShellArg serviceEnv} ${serviceDeclaration}
            )
          fi
        '';
      } // lib.listToAttrs (map (file:
        lib.nameValuePair "mergeT3Code-${file.name}" (sys.mergeWritable file)
      ) files);
      envy.machine.activationActions = map (file: {
        id = file.name;
        owner = "modules/agents/providers/t3code/default.nix";
        manager = "structured-merge";
        summary = "Merge envY agent or appearance settings into ${file.target}.";
        effects = [ "Merge declared preferences into a writable JSON file while retaining unmanaged keys." ];
        verification = "Compare the declaration with the target using the structured merge rules.";
        repeatability = "Activation reapplies the declared keys; concurrent application edits can conflict.";
        rollbackBoundary = "A generation rollback does not restore previous writable application preferences.";
        merge = {
          target = "${config.home.homeDirectory}/${file.target}";
          format = "json";
          source = toString (sys.config.mergeWritableSource { inherit (file) name data; });
          managedKeys = [];
          managedKeyPrefixes = {};
        };
      }) files ++ [ {
        id = "t3code-service";
        owner = "modules/agents/providers/t3code/default.nix";
        manager = if machinePlatform == "darwin" then "launchd" else "systemd-user";
        summary = "Merge T3 Code service.env and run the Nix server as a user service.";
        effects = [ "Merge declared network keys while retaining other environment entries."
          "Start or restart the user service when its Nix command changes." ];
        verification = "Inspect the native user service and server-runtime.json; probe its listening interfaces.";
        repeatability = "Managed fields are reapplied at activation and service startup.";
        rollbackBoundary = "Service restarts interrupt active turns; writable data is not rolled back.";
      } ];
    })
  ];
}
