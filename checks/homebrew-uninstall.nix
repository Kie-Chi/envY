{ lib, darwinConfigurations, homeConfigurations }:

let
  baseline = darwinConfigurations.macbook_air;
  configure = module: (baseline.extendModules { modules = [ module ]; }).config;
  empty = configure { envy.darwin.software.homebrew.uninstall.casks = lib.mkForce [ ]; };
  disabled = configure { homebrew.enable = lib.mkForce false; };
  caskConflict = configure { envy.darwin.software.homebrew.casks.include = [ "t3-code" ]; };
  qualifiedConflict = configure { envy.darwin.software.homebrew.casks.include = [ "homebrew/cask/t3-code" ]; };
  formulaConflict = configure {
    envy.darwin.software.homebrew.uninstall.formulae = [ "vendor/tools/retired" ];
    envy.darwin.software.homebrew.formulae.include = [ "retired" ];
  };
  excluded = configure {
    envy.darwin.software.homebrew.casks.include = [ "t3-code" ];
    envy.darwin.software.homebrew.casks.exclude = [ "t3-code" ];
  };
  invalidName = configure { envy.darwin.software.homebrew.uninstall.casks = lib.mkForce [ "--zap" ]; };
  hasConflict = kind: configuration: lib.any
    (item: lib.hasPrefix ("Homebrew " + kind + " cannot be selected") item.message && !item.assertion)
    configuration.assertions;
  hasAction = configuration: lib.any (action: action.id == "homebrew-uninstall")
    configuration.envy.machine.activationActions;
  script = configuration: configuration.system.activationScripts.homebrew.text;
  validHost = configuration:
    let cfg = configuration.config; in
    cfg.envy.machine.manifest.software.homebrew.uninstall == { casks = [ "t3-code" ]; formulae = [ ]; }
    && cfg.homebrew.onActivation.cleanup == "none"
    && !(builtins.elem "t3-code" cfg.envy.darwin.software.homebrew.casks.effective)
    && !hasConflict "casks" cfg
    && !hasConflict "formulae" cfg
    && hasAction cfg
    && lib.hasInfix ("sudo --user=" + lib.escapeShellArg cfg.homebrew.user + " --set-home") (script cfg)
    && lib.hasInfix "uninstall-homebrew.py" (builtins.head (lib.splitString "# Homebrew Bundle" (script cfg)));
in
assert lib.all validHost (builtins.attrValues darwinConfigurations);
assert !hasConflict "casks" excluded;
assert hasConflict "casks" caskConflict;
assert hasConflict "casks" qualifiedConflict;
assert hasConflict "formulae" formulaConflict;
assert !(builtins.tryEval (builtins.deepSeq invalidName.envy.darwin.software.homebrew.uninstall.casks true)).success;
assert !hasAction empty;
assert !hasAction disabled;
assert !(lib.hasInfix "uninstall-homebrew.py" (script empty));
assert !(lib.hasInfix "uninstall-homebrew.py" (script disabled));
assert lib.all (configuration: !(configuration.config.envy.machine.manifest.software ? homebrew))
  (builtins.attrValues homeConfigurations);
true
