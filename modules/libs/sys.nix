{ pkgs, lib, config, ... }:

let
  isDarwin = pkgs.stdenv.isDarwin;
  linuxSystemPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
  sopsHomePasswdPath = config.sops.secrets.home-passwd.path;
  hasSopsSecrets = config.sops.secrets != {};

  # Platform-specific sops decrypt script detection
  sopsDecryptScript =
    if !hasSopsSecrets then null
    else if isDarwin then
      (config.launchd.agents.sops-nix.config.Program or null)
    else
      let
        execStart = config.systemd.user.services.sops-nix.Service.ExecStart or null;
      in
        if execStart != null then toString (builtins.head execStart) else null;

  sys = rec {
    cmds = import ./cmds.nix { inherit pkgs; };

    initSudoPwd = ''
      SUDO_PWD=""
    '';

    esudoFn = ''
      esudo() {
        if [ -z "''${SUDO_PWD:-}" ] && [ -f "${sopsHomePasswdPath}" ]; then
          SUDO_PWD=$(cat "${sopsHomePasswdPath}")
        fi
        if [ -n "''${SUDO_PWD:-}" ]; then
          printf '%s\n' "$SUDO_PWD" | ${cmds.sudo} -S "$@"
        else
          ${cmds.sudo} "$@"
        fi
      }
    '';

    logFn = ''
      _log() {
        LEVEL="$1"
        shift
        MSG="$*"
        RESET="\033[0m"
        case "$LEVEL" in
          debug) COLOR="\033[0;36m"  ;; # cyan
          info)  COLOR="\033[0;32m"  ;; # green
          warn)  COLOR="\033[0;33m"  ;; # yellow
          error) COLOR="\033[0;31m"  ;; # red
          *)     COLOR="$RESET"      ;;
        esac
        printf "''${COLOR}[%s][%s]''${RESET} %s\n" "$LEVEL" "''${_LOG_CTX:-activation}" "$MSG" >&2
      }
      log_debug() { _log debug "$@"; }
      log_info()  { _log info  "$@"; }
      log_warn()  { _log warn  "$@"; }
      log_error() { _log error "$@"; }
    '';

    sopsDecrypt = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      _LOG_CTX="sopsDecrypt"
      ${lib.optionalString (sopsDecryptScript != null) ''
        if [ -x "${sopsDecryptScript}" ]; then
          log_info "Decrypting sops secrets..."
          PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH" "${sopsDecryptScript}" || log_warn "sops-nix decryption failed — secrets may be unavailable"
        fi
      ''}
      # Read sudo password from decrypted secret
      if [ -f "${sopsHomePasswdPath}" ]; then
        SUDO_PWD=$(cat "${sopsHomePasswdPath}")
      fi
    '';

    userBoundary = lib.hm.dag.entryAfter [ "writeBoundary" "sopsDecrypt" ] ''
      _LOG_CTX="userBoundary"
      ${lib.optionalString hasSopsSecrets ''
        if [ -f "${sopsHomePasswdPath}" ]; then
          log_info "sops secrets available"
        else
          log_warn "sops secrets unavailable — esudo will require manual password"
        fi
      ''}
      log_info "user script boundary ready"
    '';

    # --- Linux-only package management functions ---
    pkg = lib.optionalAttrs (!isDarwin) rec {
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
    };

    config = rec {
      renderers = {
        ini = attrs: lib.generators.toINI {} attrs;
        yaml = attrs: lib.generators.toYAML {} attrs;
        json = attrs: builtins.toJSON attrs;
        toml = attrs: lib.generators.toTOML {} attrs;
        plain = text: text;
        lines = values: lib.concatStringsSep "\n" values + "\n";
        kv = attrs: renderers.lines (lib.mapAttrsToList (k: v: "${k} ${toString v}") attrs);
        kvEq = attrs: renderers.lines (lib.mapAttrsToList (k: v: "${k} = ${toString v}") attrs);
      };

      renderedText = { format, data }:
        if format == "concat" then
          lib.concatMapStrings (
            item:
              if builtins.isAttrs item && item ? format && item ? data
              then renderedText item
              else throw "Invalid concat item: expected { format, data; }"
          ) data
        else if format == "ini" then renderers.ini data
        else if format == "yaml" then renderers.yaml data
        else if format == "json" then renderers.json data
        else if format == "toml" then renderers.toml data
        else if format == "plain" then data
        else if format == "lines" then renderers.lines data
        else if format == "kv" then renderers.kv data
        else if format == "kvEq" then renderers.kvEq data
        else throw "Unsupported render format: ${format}";

      renderedFile = { name, format, data }:
        pkgs.writeText name (renderedText { inherit format data; });

      deployScript = {
        source,
        target,
        owner ? "root",
        group ? "root",
        mode ? "0644",
        useSudo ? true,
        postDeploy ? ""
      }:
      let
        mkdir = if useSudo then "esudo ${cmds.mkdir}" else cmds.mkdir;
        install = if useSudo then "esudo ${cmds.install}" else cmds.install;
      in ''
        TMP_FILE="$(${cmds.mktemp})"
        cp "${source}" "$TMP_FILE"
        if [ ! -f "${target}" ] || ! ${cmds.cmp} -s "$TMP_FILE" "${target}"; then
          ${mkdir} -p "$(dirname "${target}")"
          ${install} -m ${mode} -o ${owner} -g ${group} "$TMP_FILE" "${target}"
          ${postDeploy}
        fi
        ${cmds.rm} -f "$TMP_FILE"
      '';

      deploy = {
        name,
        format,
        data,
        target,
        owner ? "root",
        group ? "root",
        mode ? "0644",
        post ? ""
      }:
        let src = renderedFile { inherit name format data; };
        in deployScript {
          source = src;
          postDeploy = post;
          inherit target owner group mode;
        };

      # Return true when every key/value in `needle` is structurally present in
      # `haystack`. Attribute key order is irrelevant; lists are compared in
      # order and must have the same length. Scalars use exact equality.
      contains = needle: haystack:
        if builtins.isAttrs needle && builtins.isAttrs haystack then
          lib.all (name:
            builtins.hasAttr name haystack
            && contains needle.${name} haystack.${name}
          ) (builtins.attrNames needle)
        else if builtins.isList needle && builtins.isList haystack then
          builtins.length needle == builtins.length haystack
          && (builtins.length needle == 0 || lib.all (index:
            contains (builtins.elemAt needle index) (builtins.elemAt haystack index)
          ) (lib.range 0 (builtins.length needle - 1)))
        else needle == haystack;

      # Merge declarations into application-owned files after Home Manager has
      # generated its links, leaving a writable user-owned result.
      mergeWritable = { name, format ? "json", data, target }:
        let
          source = renderedFile { inherit name data; format = "json"; };
          python = pkgs.python3.withPackages (ps: [ ps.pyyaml ps.tomli-w ]);
        in lib.hm.dag.entryAfter [ "linkGeneration" ] ''
          _LOG_CTX="${name}"
          if [ -z "$DRY_RUN_CMD" ]; then
            PYTHONPATH=${../../resources/scripts} ${python}/bin/python -m envy.structured_config \
              --format ${lib.escapeShellArg format} \
              --source ${lib.escapeShellArg (toString source)} \
              --target "$HOME"/${lib.escapeShellArg target}
          fi
        '';

      activation = {
        after ? [ "userBoundary" ],
        pre ? "",
        name,
        format,
        data,
        target,
        owner ? "root",
        group ? "root",
        mode ? "0644",
        post ? "",
        message ? null
      }:
        sys.task.root {
          inherit after name;
          script = ''
            ${lib.optionalString (message != null) "echo \"${message}\""}
            ${pre}
            ${deploy {
              inherit name format data target owner group mode post;
            }}
          '';
        };
    };

    # Global init activation - runs before writeBoundary
    # Defines log functions, esudo (which dynamically reads sops secret when available),
    # and (on Linux) pkg management functions.
    # Secret decryption happens in sopsDecrypt (after writeBoundary), before userBoundary.
    initActivation = lib.hm.dag.entryBefore [ "writeBoundary" ] ''
      # === Global function definitions ===
      ${logFn}
      ${initSudoPwd}
      ${esudoFn}
      ${lib.optionalString (!isDarwin) ''
        # Package management functions (Linux only)
        ${pkg.systemSudoFn or ""}
        ${pkg.detectManagerFn or ""}
        ${pkg.isInstalledFn or ""}
        ${pkg.aptFn or ""}
        ${pkg.updateFn or ""}
        ${pkg.installFn or ""}
        ${pkg.installFilesFn or ""}
      ''}
    '';

    task = rec {
      activation = {
        after ? [ "userBoundary" ],
        asRoot ? false,
        guardDryRun ? true,
        name ? "activation",
        pre ? "",
        script ? "",
        post ? "",
        message ? null
      }:
        lib.hm.dag.entryAfter after ''
          _LOG_CTX="${name}"
          ${lib.optionalString (message != null) "log_info \"${message}\""}
          ${pre}
          ${lib.optionalString guardDryRun ''
            if [ -z "$DRY_RUN_CMD" ]; then
          ''}
          ${script}
          ${lib.optionalString guardDryRun ''
            fi
          ''}
          ${post}
        '';

      root = args: activation (args // { asRoot = true; });

      mkAttr     = args: { home.activation.${args.name} = activation args; };
      mkRootAttr = args: mkAttr (args // { asRoot = true; });
    };

    mkActivation = {
      after ? [ "userBoundary" ],
      asRoot ? false,
      guardDryRun ? true,
      name ? "activation",
      script
    }:
      lib.hm.dag.entryAfter after ''
        _LOG_CTX="${name}"
        ${lib.optionalString guardDryRun ''
          if [ -z "$DRY_RUN_CMD" ]; then
        ''}
        ${script}
        ${lib.optionalString guardDryRun ''
          fi
        ''}
      '';

    contains = config.contains;
    merge = lib.recursiveUpdate;
    mergeWritable = config.mergeWritable;
    render = config.renderers;
    mkRenderedText = config.renderedText;
    mkRenderedFile = config.renderedFile;
    mkDeployScript = config.deployScript;
    deploy = config.deploy;
  };
in
{
  _module.args.sys = sys;
  home.activation = {
    sysInit = sys.initActivation;
    sopsDecrypt = sys.sopsDecrypt;
    userBoundary = sys.userBoundary;
  };
}
