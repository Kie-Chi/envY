{ sys, ... }:

let
  sshdConfig = {
    PasswordAuthentication = "yes";
    PubkeyAuthentication = "yes";
  };

in
{
  home.activation.setupSshd = sys.task.activation {
    after = [ "userBoundary" ];
    name = "setupSshd";
    script = ''
      esudo ${sys.cmds.rm} -f /etc/ssh/sshd_config.d/99-dotfiles.conf

      ${sys.deploy {
        name = "sshd_config";
        format = "kv";
        data = sshdConfig;
        target = "/etc/ssh/sshd_config.d/99-envy.conf";
        mode = "0644";
      }}

      esudo_system ${sys.cmds.systemctl} enable --now ssh
      esudo_system ${sys.cmds.systemctl} restart ssh
      log_info "sshd setup complete."
    '';
  };
}
