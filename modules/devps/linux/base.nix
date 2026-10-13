{ pkgs, config, lib, sys, ... }:

{
  envy.software.sources."native:docker" = {
    label = "Docker official repository";
    ecosystem = "native"; type = "native"; scope = "system";
    platforms = [ "linux" ]; owner = "modules/devps/linux/base.nix";
    parameters = {
      distros = [ "ubuntu" "debian" ];
      apt = {
        uri = "https://download.docker.com/linux/{distro}";
        suites = [ "{codename}" ]; components = [ "stable" ];
        keyUrl = "https://download.docker.com/linux/ubuntu/gpg";
        keyFingerprint = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88";
      };
    };
  };
  envy.linux.software.native.repositories.include = [
    { id = "docker"; name = "docker"; ref = "native:docker"; sourceId = "native:docker"; }
  ];
  envy.linux.software.native.packages.include = map (name: {
    id = name; inherit name; ref = "native:" + name; sourceId = "native:docker";
  }) [ "docker-ce" "docker-ce-cli" "containerd.io" "docker-buildx-plugin" "docker-compose-plugin" ];

  envy.software.nix.packages.include = with pkgs; [
    # env management
    mamba-cpp

    # build or pkg manager
    go
    uv
    maven
    javaPackages.compiler.openjdk21
    nodejs_26
  ];

  envy.software.nix.packages.references = {
    "mamba-cpp" = "nix:mamba-cpp";
    go = "nix:go";
    uv = "nix:uv";
    maven = "nix:maven";
    nodejs = "nix:nodejs_26";
  };

  programs.zsh = {
    initContent = ''
      # Mamba Initialization
      # This check prevents errors if mamba isn't in the PATH for some reason
      if command -v mamba &> /dev/null; then
        eval "$(mamba shell hook --shell zsh)"
      fi
    '';
  };

  home.activation.installNativeDocker = lib.mkIf (builtins.any (item: item.name == "docker-ce") config.envy.linux.software.native.packages.effective) (sys.task.root {
    name = "installNativeDocker";
    after = [ "installNativePackages" ];
    script = ''
      envy_state_dir="$HOME/.config/envy"
      legacy_state_dir="$HOME/.config/dotfiles"
      if [ -e "$legacy_state_dir/docker.installed" ] && [ ! -e "$envy_state_dir/docker.installed" ]; then
        $DRY_RUN_CMD ${sys.cmds.mkdir} -p "$envy_state_dir"
        $DRY_RUN_CMD ${sys.cmds.cp} -p "$legacy_state_dir/docker.installed" "$envy_state_dir/docker.installed"
        if [ -e "$legacy_state_dir/docker.modified" ]; then
          $DRY_RUN_CMD ${sys.cmds.cp} -p "$legacy_state_dir/docker.modified" "$envy_state_dir/docker.modified"
        fi
        $DRY_RUN_CMD ${sys.cmds.rm} -f "$legacy_state_dir/docker.installed" "$legacy_state_dir/docker.modified"
      fi

      if [ ! -e "$envy_state_dir/docker.installed" ] && [ ! -e "$legacy_state_dir/docker.installed" ]; then
        log_info "No Docker found, installing..."
        $DRY_RUN_CMD ${sys.cmds.mkdir} -p "$envy_state_dir"
        $DRY_RUN_CMD esudo ${sys.cmds.usermod} -aG docker ${config.envy.user.name}
        if [ -z "$DRY_RUN_CMD" ]; then
          if id -nG "${config.envy.user.name}" | ${sys.cmds.grep} -qw "docker"; then
              log_info "To use Docker without sudo in this terminal, you must run: 'newgrp docker'"
          fi
        else
          log_info "Docker installation and user modification dry-run completed."
        fi
        log_info "Docker installed successfully!"
        $DRY_RUN_CMD ${sys.cmds.touch} "$envy_state_dir/docker.installed"
        $DRY_RUN_CMD ${sys.cmds.touch} "$envy_state_dir/docker.modified"
      else
        if [ -z "$DRY_RUN_CMD" ]; then
          if id -nG "${config.envy.user.name}" | ${sys.cmds.grep} -qw "docker"; then
            log_info "Docker All right."
          else
            esudo ${sys.cmds.usermod} -aG docker ${config.envy.user.name}
            esudo ${sys.cmds.mkdir} -p "$envy_state_dir"
            esudo ${sys.cmds.touch} "$envy_state_dir/docker.modified"
          fi
        else
          log_info "Docker installation and user modification dry-run completed."
        fi
        log_info "Docker found, skipping installation."
      fi
    '';
    post = ''
      if [ -e "$HOME/.config/envy/docker.modified" ]; then
        log_info "Docker configuration modified."
        esudo_system ${sys.cmds.systemctl} restart docker
      fi
    '';
  });

}
