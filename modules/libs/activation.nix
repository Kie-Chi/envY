# Home Manager activation prelude shared by every feature: logging, esudo,
# sops decryption, and the task helper that feature entries are built with.
{ config, lib, pkgs, cmds, linuxPackages }:

let
  isDarwin = pkgs.stdenv.isDarwin;
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
in
rec {
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
      ${linuxPackages.systemSudoFn or ""}
      ${linuxPackages.detectManagerFn or ""}
      ${linuxPackages.isInstalledFn or ""}
      ${linuxPackages.aptFn or ""}
      ${linuxPackages.updateFn or ""}
      ${linuxPackages.installFn or ""}
      ${linuxPackages.installFilesFn or ""}
    ''}
  '';

  task = {
    activation = {
      after ? [ "userBoundary" ],
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
  };
}
