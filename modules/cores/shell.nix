{ pkgs, lib, config, machinePlatform, sys, ... }:

let
  machineEnvironment = config.envy.environment;
  machineZsh = config.envy.shell.zsh;
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
      plugins = [ "git" "sudo" "docker" "web-search" "z" "command-not-found" "colored-man-pages" "history" "pip" "python" "golang" ];
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
      {
        name = "fzf-tab";
        src = pkgs.zsh-fzf-tab;
        file = "share/fzf-tab/fzf-tab.plugin.zsh";
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

      ll = "ls -alh";
      ".." = "cd ..";
      "..." = "cd ../..";
      myip = "ip -c -br a";
      ports = "sudo ss -nultp";
      py = "python3";
      rcat = "command cat";
      grep = "rg";

    } // machineAliases;


    initContent = lib.mkMerge [
      ''
        [[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh

        ${builtins.readFile ../../files/zsh/opt.zsh}
        ${builtins.readFile ../../files/zsh/func.zsh}
      ''
      (lib.mkOrder 2000 machineZsh.initContent)
    ];
  };

  home.sessionVariables = machineSessionVariables;
  home.sessionPath = lib.mkAfter machineEnvironment.sessionPath;

  programs.zoxide = {
    enable = true;
    enableZshIntegration = true;
  };

  programs.fzf = {
    enable = true;
    enableZshIntegration = true;
  };

  home.file.".p10k.zsh".source = ../../files/zsh/p10k.zsh;

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
