# Shell functions that drive the host's native package manager on Linux.
# Activation defines them before writeBoundary; see activation.nix.
{ pkgs, cmds }:

let
  linuxSystemPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
in
{
  systemSudoFn = ''
    esudo_system() {
      esudo ${pkgs.coreutils}/bin/env \
        "PATH=${linuxSystemPath}" \
        "$@"
    }
  '';

  detectManagerFn = ''
    detect_pkg_manager() {
      if [ -x "${cmds.apt}" ] || command -v apt >/dev/null 2>&1; then
        echo "apt"
      elif [ -x "/usr/bin/dnf" ] || command -v dnf >/dev/null 2>&1; then
        echo "dnf"
      elif [ -x "/usr/bin/pacman" ] || command -v pacman >/dev/null 2>&1; then
        echo "pacman"
      elif [ -x "/usr/bin/zypper" ] || command -v zypper >/dev/null 2>&1; then
        echo "zypper"
      else
        log_error "[pkg] no supported package manager found; PATH=$PATH"
        echo "unknown"
      fi
    }
  '';

  isInstalledFn = ''
    pkg_installed() {
      PKG_MANAGER="$(detect_pkg_manager)"
      case "$PKG_MANAGER" in
        apt)
          if [ ! -x "${cmds.dpkgQuery}" ]; then
            return 1
          fi
          ${cmds.dpkgQuery} -W -f='$'"{Status}" "$1" 2>/dev/null | ${cmds.grep} -q "ok installed"
          ;;
        dnf|zypper)
          if [ ! -x "/usr/bin/rpm" ] && ! command -v rpm >/dev/null 2>&1; then
            return 1
          fi
          /usr/bin/rpm -q "$1" >/dev/null 2>&1
          ;;
        pacman)
          if [ ! -x "/usr/bin/pacman" ] && ! command -v pacman >/dev/null 2>&1; then
            return 1
          fi
          /usr/bin/pacman -Q "$1" >/dev/null 2>&1
          ;;
        *)
          return 1
          ;;
      esac
    }
  '';

  aptFn = ''
    apt_envy() {
      local APT_CMD
      APT_CMD="${cmds.apt}"
      if [ ! -x "$APT_CMD" ]; then
        APT_CMD="$(command -v apt)"
      fi
      esudo_system ${pkgs.python3}/bin/python3 -c \
        'import sys; sys.path.insert(0, "${../../resources/scripts}"); from envy.sources.native.backend import main; main()' \
        apt -- "$APT_CMD" "$@"
    }
  '';

  updateFn = ''
    pkg_update() {
      PKG_MANAGER="$(detect_pkg_manager)"
      case "$PKG_MANAGER" in
        apt)
          apt_envy update
          ;;
        dnf)
          esudo_system /usr/bin/dnf makecache -y
          ;;
        pacman)
          echo "Pacman: retaining existing sync database; run a full pacman -Syu explicitly when needed."
          ;;
        zypper)
          esudo_system /usr/bin/zypper --gpg-auto-import-keys refresh
          ;;
        *)
          echo "No supported package manager found. Skipping package index update."
          return 1
          ;;
      esac
    }
  '';

  installFn = ''
    pkg_install() {
      if [ $# -eq 0 ]; then
        return 0
      fi

      PKG_MANAGER="$(detect_pkg_manager)"
      case "$PKG_MANAGER" in
        apt)
          apt_envy install -y "$@"
          ;;
        dnf)
          esudo_system /usr/bin/dnf install -y "$@"
          ;;
        pacman)
          esudo_system /usr/bin/pacman -S --noconfirm --needed "$@"
          ;;
        zypper)
          esudo_system /usr/bin/zypper --non-interactive install "$@"
          ;;
        *)
          echo "No supported package manager found. Cannot install: $*"
          return 1
          ;;
      esac
    }
  '';

  installFilesFn = ''
    pkg_install_files() {
      if [ $# -eq 0 ]; then
        return 0
      fi

      PKG_MANAGER="$(detect_pkg_manager)"
      case "$PKG_MANAGER" in
        apt)
          apt_envy install -y "$@"
          ;;
        dnf)
          esudo_system /usr/bin/dnf install -y "$@"
          ;;
        zypper)
          esudo_system /usr/bin/zypper --non-interactive install "$@"
          ;;
        pacman)
          echo "Local package file installation is not implemented for pacman in this module."
          return 1
          ;;
        *)
          echo "No supported package manager found. Cannot install local package files: $*"
          return 1
          ;;
      esac
    }
  '';
}
