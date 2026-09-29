{ config, pkgs, lib, ... }:

{
  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    settings = {
        "*" = {
            Compression = true;
        };

        "github.com" = {
            HostName = "ssh.github.com";
            Port = 443;
            User = "git";
        };

        "nas" = {
            HostName = "nas.fl0wer.cn";
            Port = 6611;
            User = "chi";
        };

        "dell-1" = {
            HostName = "202.112.47.189";
            Port = 22;
            User = "xiongxk";
        };

        "jyue" = {
          HostName = "100.73.61.31";
          Port = 22;
          User = "jyxc-dz-0101541";
        };
    };
  };

  home.activation.generateSSHKey = lib.hm.dag.entryAfter ["userBoundary"] ''
    _LOG_CTX="generateSSHKey"
    ssh_key="$HOME/.ssh/id_ed25519"
    email="${config.programs.git.settings.user.email}"

    if [ ! -f "$ssh_key" ]; then
      log_info "Generating SSH Key for $email..."
      $DRY_RUN_CMD mkdir -p "$HOME/.ssh"
      $DRY_RUN_CMD chmod 700 "$HOME/.ssh"
      $DRY_RUN_CMD ${pkgs.openssh}/bin/ssh-keygen -t ed25519 -C "$email" -f "$ssh_key" -N ""
      log_info "SSH Key generated at $ssh_key"
    fi
  '';
}
