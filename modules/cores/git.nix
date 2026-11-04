{ lib, config, machinePlatform, ... }:

{
  programs.git = {
    enable = true;
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
    } // lib.optionalAttrs (machinePlatform == "linux") {
      core.editor = "vim";
    };
  };
}
