{ config, lib, pkgs, ... }:

let
  uninstall = lib.mapAttrs (_: lib.unique) config.envy.darwin.software.homebrew.uninstall;
  enabled = config.homebrew.enable && (uninstall.casks != [ ] || uninstall.formulae != [ ]);
  declaration = pkgs.writeText "homebrew-uninstall.json" (builtins.toJSON uninstall);
  # Core packages have both short and fully-qualified spellings.
  normalize = name: lib.removePrefix "homebrew/core/" (lib.removePrefix "homebrew/cask/" name);
  overlaps = left: right:
    let a = normalize left; b = normalize right; in
    a == b || a == builtins.baseNameOf b || builtins.baseNameOf a == b;
  conflicts = removals: selections: lib.filter
    (name: lib.any (overlaps name) selections) removals;
  formulaConflicts = conflicts uninstall.formulae
    (config.envy.darwin.software.homebrew.formulae.effective ++ map (entry: entry.name) config.homebrew.brews);
  caskConflicts = conflicts uninstall.casks (map (entry: entry.name) config.homebrew.casks);
in
{
  config = lib.mkMerge [
    {
      assertions = [
        {
          assertion = formulaConflicts == [ ];
          message = "Homebrew formulae cannot be selected for both installation and uninstall: ${lib.concatStringsSep ", " formulaConflicts}";
        }
        {
          assertion = caskConflicts == [ ];
          message = "Homebrew casks cannot be selected for both installation and uninstall: ${lib.concatStringsSep ", " caskConflicts}";
        }
      ];
    }
    (lib.mkIf enabled {
      # Use the same user as nix-darwin's Homebrew bundle invocation.
      system.activationScripts.homebrew.text = lib.mkBefore ''
        echo >&2 "Homebrew explicit uninstall..."
        if [ ! -x ${lib.escapeShellArg "${config.homebrew.prefix}/bin/brew"} ]; then
          echo >&2 "error: Homebrew is unavailable; cannot reconcile the explicit uninstall list."
          exit 1
        fi
        sudo --user=${lib.escapeShellArg config.homebrew.user} --set-home \
          ${pkgs.python3.interpreter} ${./uninstall-homebrew.py} \
          ${lib.escapeShellArg "${config.homebrew.prefix}/bin/brew"} ${declaration}
      '';
      envy.machine.activationActions = [{
        id = "homebrew-uninstall";
        owner = "modules/software/darwin/homebrew.nix";
        manager = "homebrew";
        summary = "Uninstall only explicitly named Homebrew casks and formulae before Homebrew bundle."
          + " Casks: ${lib.concatStringsSep ", " uninstall.casks}; formulae: ${lib.concatStringsSep ", " uninstall.formulae}.";
        effects = [ "Run brew uninstall only for installed entries in the explicit list, without zap, force, or autoremove." ];
        verification = "Compare the explicit list with brew list --full-name for each package kind.";
        repeatability = "Already absent entries are skipped; inventory or uninstall failures abort activation.";
        rollbackBoundary = "Generation rollback does not reinstall removed Homebrew packages or undo cask uninstall hooks.";
      }];
    })
  ];
}
