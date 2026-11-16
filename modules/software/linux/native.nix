{ config, lib, pkgs, sys, ... }:

let
  packages = config.envy.linux.software.native.packages.effective;
  descriptor = pkgs.writeText "envy-native-packages.json" (builtins.toJSON {
    sources = config.envy.software.sources; inherit packages;
  });
  sourceInstaller = pkgs.writeShellApplication {
    name = "envy-native-install";
    runtimeInputs = [ pkgs.python3 pkgs.gnupg ];
    text = ''
      exec python3 -c 'import sys; sys.path.insert(0, "${../../../resources/scripts}"); from envy.sources.native.backend import main; main()' install --definitions "${descriptor}"
    '';
  };
  packageNameFor = item: manager:
    let names = item.parameters.names or { };
    in names.${manager} or item.name;
  resolvePackage = item:
    if (item.parameters.resolver or null) == "current-kernel-headers"
    then ''
      case "$PKG_MANAGER" in
        apt) PKG="linux-headers-$(uname -r)" ;;
        dnf) PKG="kernel-devel-$(uname -r)" ;;
        pacman) PKG="linux-headers" ;;
        zypper) PKG="kernel-devel" ;;
        *) PKG=${lib.escapeShellArg item.name} ;;
      esac
    ''
    else ''
      case "$PKG_MANAGER" in
        apt) PKG=${lib.escapeShellArg (packageNameFor item "apt")} ;;
        dnf) PKG=${lib.escapeShellArg (packageNameFor item "dnf")} ;;
        pacman) PKG=${lib.escapeShellArg (packageNameFor item "pacman")} ;;
        zypper) PKG=${lib.escapeShellArg (packageNameFor item "zypper")} ;;
        *) PKG=${lib.escapeShellArg item.name} ;;
      esac
    '';
  collectPackage = item: lib.optionalString (item.sourceId == null || item.sourceId == "native:system") ''
    ${resolvePackage item}
    if pkg_installed "$PKG"; then
      log_info "'$PKG' already installed, skip."
    else
      log_warn "'$PKG' not found, queued for install."
      MISSING_PKGS+=("$PKG")
    fi
  '';
in
{
  envy.machine.activationActions = lib.optional (packages != []) {
    id = "native-packages";
    owner = "modules/software/linux/native.nix";
    manager = "native-package-manager";
    summary = "Ensure ${toString (builtins.length packages)} selected native packages are installed with the detected Linux package manager.";
    effects = [ "Query installed packages, refresh package metadata when needed, and install missing system or source-constrained packages with elevated privileges; package scripts may run." ];
    verification = "Compare effective package selections with installed package state; metadata inspection does not run package installation or refresh repositories.";
    repeatability = "Installed packages are skipped; package names and current-kernel headers may resolve differently on the running host. This description does not authorize a retry.";
    rollbackBoundary = "A generation rollback does not remove native packages, restore package versions or dependencies, or undo package scripts and service changes.";
  };
  home.activation.installNativePackages = sys.task.activation {
    name = "native-packages";
    after = [ "configureNativeRepositories" ];
    pre = ''
      MISSING_PKGS=()
      log_debug "starting native package installation"
    '';
    script = ''
      PKG_MANAGER="$(detect_pkg_manager)"
      log_debug "detected package manager: $PKG_MANAGER"
      ${lib.concatMapStringsSep "\n" collectPackage packages}

      if [ ''${#MISSING_PKGS[@]} -gt 0 ]; then
        pkg_update
        pkg_install "''${MISSING_PKGS[@]}"
      else
        log_info "all native system packages satisfied."
      fi
      esudo_system ${sourceInstaller}/bin/envy-native-install
    '';
  };
}
