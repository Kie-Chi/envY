{ pkgs, lib, config, machinePlatform, sys, ... }:

let
  machineEnvironment = config.envy.environment;
  machineZsh = config.envy.shell.zsh;
  selectedPackage = name: lib.findFirst
    (package: lib.getName package == name)
    null
    config.envy.software.nix.packages.effective;
  ezaSelected = selectedPackage "eza" != null;
  fzfPackage = selectedPackage "fzf";
  zoxidePackage = selectedPackage "zoxide";
  atuinPackage = selectedPackage "atuin";
  atuinSelected = atuinPackage != null;
  fzfSelected = fzfPackage != null;
  zoxideSelected = zoxidePackage != null;
  palette = import ./tokyo-night.nix;
  fzfColors = lib.concatStringsSep "," (lib.mapAttrsToList (name: value: "${name}:${value}") palette.fzf);
  machineSessionVariables = lib.mapAttrs
    (_: value: lib.mkOverride 90 value)
    machineEnvironment.sessionVariables;
  machineAliases = lib.mapAttrs
    (_: value: lib.mkOverride 90 value)
    machineZsh.aliases;
in
{
  programs.zsh = {
    enable = true;
    autosuggestion.enable = true;
    syntaxHighlighting.enable = true;
    historySubstringSearch.enable = true;

    oh-my-zsh = {
      enable = true;
      plugins = [ "git" "sudo" "docker" "web-search" "command-not-found" "colored-man-pages" "history" "pip" "python" "golang" ];
    };

    plugins = [
      {
        name = "powerlevel10k";
        src = pkgs.zsh-powerlevel10k;
        file = "share/zsh-powerlevel10k/powerlevel10k.zsh-theme";
      }
      {
        name = "zsh-completions";
        src = pkgs.zsh-completions;
      }
      {
        name = "conda-zsh-completion";
        src = pkgs.fetchFromGitHub {
          owner = "esc";
          repo = "conda-zsh-completion";
          rev = "c3dca5b56cca978d974f821f592ee8f11c32320d";
          hash = "sha256-4JaoYv1dbUaOs5YxZtlG+Q21CYMk2d0EmPfUdKdoNA4=";
        };
        file = "conda-zsh-completion.plugin.zsh";
      }
    ];

    sessionVariables = {
      LANG = "en_US.UTF-8";
      EDITOR = "code";
      ENVY_ROOT = config.envy.repository.path;
    } // machineSessionVariables;

    shellAliases = {
      zshconf = "nvim ${config.envy.repository.path}/modules/cores/shell.nix";
      omzconf = "nvim ~/.oh-my-zsh";

      ll = if ezaSelected then "eza --all --long --header" else "ls -alh";
      ".." = "cd ..";
      "..." = "cd ../..";
      myip = "ip -c -br a";
      ports = "sudo ss -nultp";
      py = "python3";
      rcat = "command cat";
      grep = "rg";

    } // machineAliases;


    initContent = lib.mkMerge [
      (lib.mkIf zoxideSelected (lib.mkOrder 851 ''
        eval "$(${lib.getExe zoxidePackage} init zsh)"
      ''))
      (lib.mkIf fzfSelected (lib.mkOrder 910 ''
        if [[ $options[zle] = on ]]; then
          source <(${lib.getExe fzfPackage} --zsh)
        fi
      ''))
      (lib.mkIf atuinSelected (lib.mkOrder 920 ''
        if [[ $options[zle] = on ]]; then
          typeset -i _envy_had_suggest_strategy=''${+ZSH_AUTOSUGGEST_STRATEGY}
          typeset -a _envy_suggest_strategy=("''${ZSH_AUTOSUGGEST_STRATEGY[@]}")
          source <(${lib.getExe atuinPackage} init zsh --disable-up-arrow)
          if (( _envy_had_suggest_strategy )); then
            ZSH_AUTOSUGGEST_STRATEGY=("''${_envy_suggest_strategy[@]}")
          else
            unset ZSH_AUTOSUGGEST_STRATEGY
          fi
          unset _envy_had_suggest_strategy _envy_suggest_strategy
        fi
      ''))
      # Bind Tab after completion and widget integrations, before highlighting.
      (lib.mkIf fzfSelected (lib.mkOrder 1155 ''
        source ${pkgs.zsh-fzf-tab}/share/fzf-tab/fzf-tab.plugin.zsh
      ''))
      ''
        [[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh

        ${lib.optionalString fzfSelected ''
          ${builtins.readFile ../../files/zsh/fzf-preview.zsh}
          zstyle ':fzf-tab:*' fzf-flags ${lib.escapeShellArg "--color=${fzfColors}"} '--border=rounded' '--preview-window=right:55%:wrap:<50(down:45%:wrap)'
        ''}

        ${builtins.readFile ../../files/zsh/opt.zsh}
        ${builtins.readFile ../../files/zsh/func.zsh}
      ''
      (lib.mkOrder 2000 machineZsh.initContent)
    ];
  };

  home.sessionVariables = lib.mkMerge [
    machineSessionVariables
    (lib.mkIf fzfSelected {
      FZF_DEFAULT_OPTS = "--color=${fzfColors}";
    })
  ];
  home.sessionPath = lib.mkAfter machineEnvironment.sessionPath;

  envy.software.nix.packages.include = [ pkgs.eza pkgs.fzf pkgs.zoxide pkgs.atuin ];
  envy.software.nix.packages.references = {
    eza = "nix:eza";
    fzf = "nix:fzf";
    zoxide = "nix:zoxide";
    atuin = "nix:atuin";
  };

  programs.eza = {
    enable = ezaSelected;
    package = null;
    enableZshIntegration = true;
    enableBashIntegration = false;
    enableFishIntegration = false;
    enableIonIntegration = false;
    icons = "auto";
    git = true;
    extraOptions = [ "--group-directories-first" ];
  };

  # The locked modules cannot disable package installation. envY owns it,
  # while the selected-gated Zsh integrations above retain their original order.
  programs.zoxide.enable = false;
  programs.fzf.enable = false;

  home.file.".p10k.zsh".source = ../../files/zsh/p10k.zsh;

  # envY owns installation; only the selected tool contributes configuration.
  programs.atuin.enable = false;
  xdg.configFile = {
    "eza/theme.yml" = lib.mkIf ezaSelected {
      source = pkgs.vimPlugins.tokyonight-nvim.src + "/extras/eza/tokyonight_night.yml";
    };
    "atuin/config.toml" = lib.mkIf atuinSelected {
      text = ''
        auto_sync = false
        update_check = false
        enter_accept = false
        exit_mode = "return-original"
        [sync]
        records = false
        [daemon]
        enabled = false
        autostart = false
        [logs]
        enabled = false
        [theme]
        name = "envy-night"
        debug = false
      '';
    };
    "atuin/themes/envy-night.toml" = lib.mkIf atuinSelected {
      text = ''
        [theme]
        name = "envy-night"
        [colors]
        Base = "@reset"
        Annotation = "@reset"
        Muted = "@reset"
        AlertInfo = "@cyan"
        AlertWarn = "@yellow"
        AlertError = "@red"
        Guidance = "@green"
        Important = "@blue"
        Title = "@magenta"
      '';
    };
  };

  home.activation.setZshAsDefault = lib.mkIf (machinePlatform == "linux") (sys.task.activation {
    name = "setZshAsDefault";
    script = ''
      zsh_path="${config.home.profileDirectory}/bin/zsh"
      if [ "$SHELL" != "$zsh_path" ]; then
        log_info "Setting Zsh as default shell..."
        if ! ${sys.cmds.grep} -q "$zsh_path" /etc/shells; then
          printf '%s\n' "$zsh_path" | esudo ${sys.cmds.tee} -a /etc/shells > /dev/null
        fi
        esudo ${sys.cmds.usermod} -s "$zsh_path" "${config.envy.user.name}"
        log_info "Default shell changed to Zsh. Please relogin."
      fi
    '';
  });
}
