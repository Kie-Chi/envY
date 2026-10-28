{ config, lib, sys, ... }:

{
  config = lib.mkIf (!(builtins.elem "raycast" config.envy.darwin.software.homebrew.casks.exclude)) {
  envy.machine.habits = [
    {
      id = "global-launcher";
      label = "Global launcher";
      gesture = config.envy.habits.globalLauncher.gesture;
      semantic = "Open the global launcher and search surface.";
      context = "darwin";
      backend = "Raycast";
      binding = config.envy.habits.globalLauncher.gesture;
      ownership = "application";
      note = "Raycast owns its global hotkey; envY records but does not overwrite the application preference.";
      requirements = [
        {
          group = "homebrew.system.cask";
          item = "raycast";
        }
      ];
    }
  ];

  # --- sops template: raycast AI providers with encrypted keys ---
  sops.templates."raycast-providers" = {
    path = "${config.home.homeDirectory}/.config/raycast/ai/providers.yaml";
    mode = "0644";
    content = ''
      providers:
        - id: stepfun
          name: StepFun
          base_url: https://stepfun.com
          api_keys:
            stepfun: ${config.sops.placeholder.llm-credential-stepfun}
          models:
            - id: step-3.7-flash
              name: Step 3.7 Flash
              context: 256000
              abilities:
                temperature:
                  supported: true
                vision:
                  supported: true
                system_message:
                  supported: true
                tools:
                  supported: true
        - id: deepseek
          name: DeepSeek
          base_url: https://api.deepseek.com
          api_keys:
            raycast: ${config.sops.placeholder.llm-credential-deepseek}
          models:
            - id: deepseek-v4-pro
              name: DeepSeek V4 Pro
              context: 200000
              abilities:
                temperature:
                  supported: true
                vision:
                  supported: true
                system_message:
                  supported: true
                tools:
                  supported: true
    '';
  };

  home.activation.fixRaycastProvidersOwnership =
    lib.hm.dag.entryBetween [ "sopsDecrypt" ] [ "writeBoundary" ] ''
    _LOG_CTX="fixRaycastProvidersOwnership"
    TARGET="$HOME/.config/raycast/ai/providers.yaml"
    if [ -e "$TARGET" ] && [ ! -O "$TARGET" ]; then
      log_info "Fixing ownership for existing Raycast providers file"
      esudo ${sys.cmds.chown} "$(${sys.cmds.id} -u):$(${sys.cmds.id} -g)" "$TARGET"
    fi
    '';
  };
}
