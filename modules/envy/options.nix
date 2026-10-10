{ lib, pkgs, machineId ? "unknown", machinePlatform ? "unknown", machineSystem ? "unknown", ... }:

let
  inherit (lib) mkOption types;
  inherit (import ./selection-options.nix { inherit lib; }) packageSelection itemSelection;
  targetPlatform =
    if pkgs.stdenv.hostPlatform.isDarwin then "darwin"
    else if pkgs.stdenv.hostPlatform.isLinux then "linux"
    else "unknown";

in
{
  options.envy = {
    machine = {
      id = mkOption {
        type = types.strMatching "[A-Za-z0-9][A-Za-z0-9_-]*";
        default = machineId;
        readOnly = true;
        description = "Machine identifier derived from the host module filename.";
      };
      platform = mkOption {
        type = types.enum [ "darwin" "linux" "unknown" ];
        default = machinePlatform;
        readOnly = true;
        description = "Platform selected by the hosts/darwin or hosts/linux directory.";
      };
      system = mkOption {
        type = types.str;
        default = machineSystem;
        readOnly = true;
        description = "Nix system selected for this platform configuration.";
      };
      manifest = mkOption {
        type = types.attrs;
        readOnly = true;
        description = "Normalized evaluated machine policy for envY tooling.";
      };
      habits = mkOption {
        type = types.listOf (types.submodule {
          options = {
            id = mkOption {
              type = types.strMatching "[a-z][a-z0-9-]*";
              description = "Stable semantic habit identifier.";
            };
            label = mkOption {
              type = types.nonEmptyStr;
              description = "Human-readable habit label.";
            };
            gesture = mkOption {
              type = types.nonEmptyStr;
              description = "Canonical user gesture, independent of platform key syntax.";
            };
            semantic = mkOption {
              type = types.nonEmptyStr;
              description = "User-facing action semantics, such as toggling a scratchpad.";
            };
            context = mkOption {
              type = types.nonEmptyStr;
              description = "Platform or desktop-session context that implements the habit.";
            };
            backend = mkOption {
              type = types.nonEmptyStr;
              description = "Application or compositor backend used in this context.";
            };
            binding = mkOption {
              type = types.nonEmptyStr;
              description = "Platform-native key binding or hotkey representation.";
            };
            ownership = mkOption {
              type = types.enum [ "declarative" "application" ];
              description = "Whether envY declares the binding or only records an application-owned preference.";
            };
            note = mkOption {
              type = types.str;
              default = "";
              description = "Short implementation note shown by envY inspection commands.";
            };
            requirements = mkOption {
              type = types.listOf (types.submodule {
                options = {
                  group = mkOption {
                    type = types.nonEmptyStr;
                    description = "Canonical evaluated software group required by this implementation.";
                  };
                  item = mkOption {
                    type = types.nonEmptyStr;
                    description = "Stable software item ID required by this implementation.";
                  };
                };
              });
              default = [ ];
              description = "Software selected by the machine policy for this implementation.";
            };
          };
        });
        default = [ ];
        internal = true;
        description = ''
          Module-owned implementations of stable personal interaction habits.
          Their gestures come from envy.habits machine policy; this option only
          aggregates implementation facts for the evaluated manifest.
        '';
      };
    };

    user = {
      name = mkOption {
        type = types.nonEmptyStr;
        description = "Local account managed by nix-darwin or Home Manager.";
      };
      home = mkOption {
        type = types.nonEmptyStr;
        description = "Absolute home directory for the managed local account.";
      };
    };

    repository.path = mkOption {
      type = types.nonEmptyStr;
      description = "Absolute path to the envY checkout on the selected machine.";
    };

    environment = {
      sessionVariables = mkOption {
        type = types.attrsOf (types.oneOf [
          types.str
          types.path
          types.int
          types.float
          types.bool
        ]);
        default = { };
        description = ''
          Non-sensitive machine-specific variables added to the Home Manager
          session and interactive Zsh environment. Machine values take
          precedence over shared defaults with the same name.
        '';
      };
      sessionPath = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Machine-specific paths appended to the Home Manager session PATH.
        '';
      };
    };

    shell.zsh = {
      aliases = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = ''
          Machine-specific Zsh aliases merged with shared aliases. Machine
          values take precedence over shared aliases with the same name.
        '';
      };
      initContent = mkOption {
        type = types.lines;
        default = "";
        description = ''
          Non-sensitive machine-specific Zsh initialization appended after
          shared and feature-owned initialization.
        '';
      };
    };

    vscode.mode = mkOption {
      type = types.enum [ "remote" "local" ];
      default = "remote";
      description = "Whether VS Code settings are managed locally or by Settings Sync.";
    };

    software = {
      nix.packages = packageSelection "cross-platform Home Manager packages";
      npm.tools = itemSelection "cross-platform user-level NPM tools";
      pypi.tools = itemSelection "cross-platform user-level PyPI tools installed by uv";
    };

    mirrors.mode = mkOption {
      type = types.enum [ "upstream" "china" ];
      default = "china";
      description = "Network mirror policy used by bootstrap and managed package ecosystems.";
    };

    mirrors.overrides = mkOption {
      type = types.attrs;
      default = { };
      description = ''
        envY-generated per-ecosystem mirror overrides. Values are written by
        `envy mirror set` into the selected machine's managed mirror block;
        they take precedence over the selected mirror profile.
      '';
    };

    habits = {
      terminalScratchpad.gesture = mkOption {
        type = types.enum [ "F2" "F3" "F4" "F5" "F6" "F7" "F8" "F9" "F10" "F12" ];
        default = "F12";
        description = ''
          Desired terminal scratchpad gesture. F1 remains the Niri screenshot
          shortcut and F11 remains a platform-specific action.
        '';
      };
      globalLauncher.gesture = mkOption {
        type = types.strMatching "Option\\+([A-Za-z0-9]|Space|Return|Tab|Escape|F([1-9]|1[0-2]))";
        default = "Option+Space";
        description = ''
          Desired global-launcher gesture in the user's cross-platform Option
          notation. Desktop modules render their native modifier representation.
        '';
      };
    };

    git = {
      name = mkOption {
        type = types.nonEmptyStr;
        description = "Git author name used on the selected machine.";
      };
      email = mkOption {
        type = types.nonEmptyStr;
        description = "Git author email used on the selected machine.";
      };
    };

    llm = {
      steps = {
        url = mkOption {
          type = types.nonEmptyStr;
          default = "https://stepfun.com";
          internal = true;
          description = "Non-sensitive StepFun-compatible API base URL.";
        };
        model = mkOption {
          type = types.nonEmptyStr;
          default = "step-3.7-flash";
          internal = true;
          description = "Default StepFun-compatible model.";
        };
      };
      deepseek = {
        url = mkOption {
          type = types.nonEmptyStr;
          default = "https://api.deepseek.com";
          internal = true;
          description = "Non-sensitive DeepSeek API base URL.";
        };
        model = mkOption {
          type = types.nonEmptyStr;
          default = "deepseek-v4-pro";
          internal = true;
          description = "Default DeepSeek model.";
        };
      };
    };
  };

  config.assertions = [
    {
      assertion = machinePlatform == "unknown" || machinePlatform == targetPlatform;
      message = "machinePlatform '${machinePlatform}' does not match target platform '${targetPlatform}'";
    }
    {
      assertion = machineSystem == "unknown" || machineSystem == pkgs.stdenv.hostPlatform.system;
      message = "machineSystem '${machineSystem}' does not match target system '${pkgs.stdenv.hostPlatform.system}'";
    }
  ];
}
