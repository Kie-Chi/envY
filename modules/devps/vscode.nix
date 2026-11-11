{ config, pkgs, lib, machinePlatform, sys, ... }:

let
  mode = config.envy.vscode.mode;
  useLocalConfig = mode == "local";
  selected = machinePlatform != "linux" || config.envy.linux.option == "desktop";
  enabled = selected && !(builtins.elem "vscode" config.envy.software.nix.packages.exclude);
in
{
  config = lib.mkMerge [
  (lib.mkIf selected {
    envy.software.nix.packages.include = [ pkgs.vscode ];
    envy.software.nix.packages.references.vscode = "nix:vscode";
  })
  (lib.mkIf enabled {
  programs.vscode = {
    enable = true;
    # Installation is owned by envy.software.nix.packages so machine exclusions apply.
    package = null;
    mutableExtensionsDir = !useLocalConfig;
    profiles.default = lib.mkIf useLocalConfig {
      userSettings = {
        "security.workspace.trust.untrustedFiles" = "open";
        "files.autoSave" = "afterDelay";
        "files.autoGuessEncoding" = true;
        "explorer.confirmDragAndDrop" = false;
        "workbench.iconTheme" = "vscode-icons";
        "workbench.colorTheme" = "Tokyo Night";
        "workbench.editor.autoLockGroups"."mainThreadWebview-markdown.preview" = true;
        "workbench.editorAssociations" = {
          "*.copilotmd" = "vscode.markdown.preview.editor";
          "{hexdiff}:/**/*.*" = "hexEditor.hexedit";
          "*.hex" = "hexEditor.hexedit";
          "*.bin" = "hexEditor.hexedit";
        };

        "editor.fontSize" = 18;
        "editor.fontFamily" = "'Maple Mono NF CN', Consolas, 'Courier New', monospace";
        "editor.fontWeight" = "450";
        "editor.fontLigatures" = false;
        "editor.fontVariations" = false;
        "editor.wordWrap" = "on";
        "editor.formatOnPaste" = true;
        "editor.minimap.enabled" = false;
        "editor.unicodeHighlight.nonBasicASCII" = false;
        "scm.inputFontFamily" = "editor";

        "terminal.integrated.copyOnSelection" = true;
        "terminal.integrated.fontSize" = 16;
        "terminal.integrated.inheritEnv" = false;
        "terminal.integrated.suggest.enabled" = false;
        "terminal.integrated.defaultProfile.linux" = null;

        "markdown-preview-enhanced.liveUpdate" = true;
        "markdown-preview-enhanced.alwaysShowBacklinksInPreview" = false;
        "markdown-preview-enhanced.enablePreviewZenMode" = false;

        "python.terminal.executeInFileDir" = true;
        "C_Cpp.default.compilerPath" = "";
        "code-runner.saveAllFilesBeforeRun" = true;
        "code-runner.saveFileBeforeRun" = true;
        "code-runner.runInTerminal" = true;
        "c-cpp-compile-run.run-in-external-terminal" = true;

        "remote.SSH.localServerDownload" = "always";
        "remote.downloadExtensionsLocally" = true;
        "remote.SSH.remotePlatform" = {
          "connect.nmb1.seetacloud.com" = "linux";
          "os-lab" = "linux";
          "chi" = "linux";
          "Firefly" = "linux";
          "59.110.55.234" = "linux";
          "debian" = "linux";
          "debian-8" = "linux";
          "debian-2" = "linux";
          "202.112.47.59" = "linux";
        };
        "remote.SSH.serverInstallPath"."os-lab" = "/23371265";

        "chat.commandCenter.enabled" = false;
        "chat.viewSessions.orientation" = "stacked";
        "github.copilot.enable" = {
          "*" = true;
          plaintext = false;
          markdown = true;
          scminput = false;
          c = true;
        };
        "github.copilot.nextEditSuggestions.enabled" = true;
        "gitlens.ai.model" = "vscode";
        "gitlens.ai.vscode.model" = "copilot:gpt-4.1";
        "claudeCode.preferredLocation" = "panel";
        "claudeCode.selectedModel" = "default";

        "vsicons.dontShowNewVersionMessage" = true;
        "redhat.telemetry.enabled" = true;
        "hediet.vscode-drawio.resizeImages" = null;
      };

      keybindings = [
        {
          key = "shift+enter";
          command = "workbench.action.terminal.sendSequence";
          args = {
            text = "\u001b\r";
          };
          when = "terminalFocus";
        }
      ];

      languageSnippets = {
        c = {
          main = {
            prefix = "main";
            body = [
              "#include <stdio.h>"
              ""
              "int main(void) {"
              "    $0"
              "    return 0;"
              "}"
            ];
            description = "C main function";
          };
        };
      };

      extensions = with pkgs.vscode-extensions; [
        anthropic.claude-code
        eamodio.gitlens
        enkia.tokyo-night
        formulahendry.code-runner
        github.copilot
        github.copilot-chat
        hediet.vscode-drawio
        ms-python.python
        ms-vscode.cpptools
        ms-vscode.hexeditor
        ms-vscode-remote.remote-containers
        ms-vscode-remote.remote-ssh
        redhat.vscode-yaml
        shd101wyy.markdown-preview-enhanced
        vscode-icons-team.vscode-icons
      ];
    };
  };

  home.activation.vscodeRemoteSyncNotice = lib.mkIf (mode == "remote") (sys.task.activation {
    name = "vscodeRemoteSyncNotice";
    script = ''
      log_info "VS Code is in remote sync mode; sign in to VS Code and enable Settings Sync to manage settings and extensions."
    '';
  });
  })
  ];
}
