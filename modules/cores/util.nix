{ config, pkgs, lib, ... }:

let
  # The flake source is already filtered and materialized. Creating another
  # cleanSource here races under Nix 2.34 when flake outputs evaluate in parallel.
  scriptsSrc = ../../resources/scripts;

  # Package single-file scripts, skipping the "envy" entry (it's now a Python package directory)
  packageScriptsFromDir = dirPath:
    let dirContents = builtins.readDir dirPath;
    in
    lib.mapAttrsToList
      (scriptName: fileType:
        if builtins.elem scriptName [ "envy" ] then
          null  # handled by a dedicated package/module
        else if fileType == "regular" then
          pkgs.writeShellScriptBin scriptName (builtins.readFile (dirPath + "/${scriptName}"))
        else
          null
      )
      dirContents;

  # Python environment with all envy dependencies
  envyPythonEnv = pkgs.python3.withPackages (ps: [
    ps.typer
    ps.rich
    ps.prompt-toolkit
    ps.pyyaml
    ps.tomli-w
  ]);

  # Special package for envy Python CLI — writeShellApplication wraps it with all runtime deps
  envyPackage = pkgs.writeShellApplication {
    name = "envy";
    runtimeInputs = [ envyPythonEnv envyTuiPackage ] ++ (with pkgs; [
      # External tools used by envy commands
      git
      nix
      sops
      age
      ssh-to-age
      jq
      gnupg
      curl
    ]);
    text = ''
      bundled="${scriptsSrc}"
      repo="''${ENVY_ROOT:-''${ENVY_DOTFILES:-''${DOTFILES_DIR:-}}}"
      if [ ! -f "$repo/flake.nix" ] || [ ! -d "$repo/resources/scripts/envy" ]; then
        repo=${lib.escapeShellArg config.envy.repository.path}
      fi
      export ENVY_ROOT="$repo"

      # Prefer repo source unless explicitly using bundled
      if [ -d "$repo/resources/scripts/envy" ] && [ "''${ENVY_USE_BUNDLED:-0}" != "1" ]; then
        export PYTHONPATH="$repo/resources/scripts:$bundled:''${PYTHONPATH:-}"
      else
        export PYTHONPATH="$bundled:''${PYTHONPATH:-}"
      fi

      if [ "''${1:-}" = "tui" ] && [ "$#" -eq 1 ] \
        && [ -z "''${_ENVY_COMPLETE:-}" ] \
        && command -v envy-tui >/dev/null 2>&1; then
        shift
        # Keep TUI probes and workflows on this wrapper's selected checkout
        # rather than an unrelated `envy` earlier on PATH.
        export ENVY_BIN="''${ENVY_BIN:-$(command -v envy || true)}"
        if [ -z "$ENVY_BIN" ]; then
          export ENVY_BIN="envy"
        fi
        exec envy-tui "$@"
      fi

      exec python3 -m envy "$@"
    '';
  };

  envyTuiPackage = pkgs.rustPlatform.buildRustPackage {
    pname = "envy-tui";
    version = "0.1.0";
    src = ../../resources/scripts/envy-tui;
    cargoLock.lockFile = ../../resources/scripts/envy-tui/Cargo.lock;
    meta = {
      description = "Ratatui frontend for the envY configuration manager";
      mainProgram = "envy-tui";
    };
  };

  packagedScripts = lib.filter (x: x != null) (packageScriptsFromDir scriptsSrc);

in
{
  envy.software.nix.packages.include = packagedScripts ++ [ envyPackage envyTuiPackage ];

  envy.software.nix.packages.references = {
    "mirror-env.sh" = "local:resources/scripts/mirror-env.sh";
    "flake-source-mirror.sh" = "local:resources/scripts/flake-source-mirror.sh";
    "nix-trust.sh" = "local:resources/scripts/nix-trust.sh";
    "nix-darwin-etc.sh" = "local:resources/scripts/nix-darwin-etc.sh";
    ppack = "local:resources/scripts/ppack";
    scrctl = "local:resources/scripts/scrctl";
    spk = "local:resources/scripts/spk";
    envy = "local:modules/cores/util.nix#envyPackage";
    "envy-tui" = "local:modules/cores/util.nix#envyTuiPackage";
  };
}
