{ pkgs, lib, config, machinePlatform, ... }:

let
  deltaPackage = lib.findFirst
    (package: lib.getName package == "delta")
    null
    config.envy.software.nix.packages.effective;
  deltaSelected = deltaPackage != null;
  palette = import ./tokyo-night.nix;
in
{
  envy.software.nix.packages.include = [ pkgs.delta ];
  envy.software.nix.packages.references.delta = "nix:delta";
  programs.delta.enable = false;

  xdg.configFile."delta/config" = lib.mkIf deltaSelected {
    text = builtins.readFile (pkgs.vimPlugins.tokyonight-nvim.src + "/extras/delta/tokyonight_night.gitconfig")
      + lib.generators.toGitINI {
        delta = {
          syntax-theme = "tokyonight_night";
          line-numbers = true;
          line-numbers-zero-style = palette.fzf.info;
          line-numbers-minus-style = builtins.elemAt palette.ansi 1;
          line-numbers-plus-style = builtins.elemAt palette.ansi 2;
          wrap-max-lines = "unlimited";
        };
      };
  };
  programs.git = {
    enable = true;
    includes = lib.mkIf deltaSelected [{ path = "${config.xdg.configHome}/delta/config"; }];
    settings = {
      user = {
        name = config.envy.git.name;
        email = config.envy.git.email;
      };

      alias = {
        st = "status";
        ci = "commit";
        co = "checkout";
        br = "branch";
        df = "diff";
        dif = "diff";
        rt = "remote";
        pl = "pull";
        ps = "push";
        cm = "commit -m";
        lg = "log --color --graph --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr) %C(bold blue)<%an>%Creset' --abbrev-commit";
      };

      color = {
        ui = true;
      };
    } // lib.optionalAttrs deltaSelected {
      pager = {
        diff = lib.getExe deltaPackage;
        log = lib.getExe deltaPackage;
        show = lib.getExe deltaPackage;
      };
    } // lib.optionalAttrs (machinePlatform == "linux") {
      core.editor = "vim";
    };
  };
}
