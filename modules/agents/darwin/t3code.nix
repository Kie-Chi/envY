{ config, lib, osConfig, pkgs, sys, ... }:

let
  casks = osConfig.envy.darwin.software.homebrew.casks.effective;
  selected = builtins.elem "t3-code" casks;
  packages = map lib.getName config.envy.software.nix.packages.effective;
  profileBin = "/etc/profiles/per-user/${config.envy.user.name}/bin";
  palette = import ../../cores/tokyo-night.nix;
  prompts = import ../prompts.nix { inherit lib pkgs; };
  # Home Manager normalizes repeatable Ghostty settings to lists.
  settingValue = value: if builtins.isList value then builtins.head value else value;
  font = settingValue (config.programs.ghostty.settings.font-family or "Maple Mono NF CN");
  fontSize = settingValue (config.programs.ghostty.settings.font-size or 13);
  codexSelected = builtins.elem "codex" casks;
  selections = import ../providers/codex/models.nix { inherit lib; } {
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
        binaryPath = "/opt/homebrew/bin/codex";
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
  config = lib.mkIf selected {
    home.activation = lib.listToAttrs (map (file:
      lib.nameValuePair "mergeT3Code-${file.name}" (sys.mergeWritable file)
    ) files);
    envy.machine.activationActions = map (file: {
      id = file.name;
      owner = "modules/agents/darwin/t3code.nix";
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
    }) files;
  };
}
