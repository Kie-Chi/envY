{ inputs }:

let
  # Exercise the real flake output selection without building Home Manager.
  # Keep legacy filesystem state isolated from the evaluator's own machine.
  legacyHome = "/envy-legacy-compatibility-test";
  legacyPath = legacyHome + "/.config/dotfiles/config.nix";
  configurations = present: legacyValue:
    let
      flake = builtins.scopedImport {
        builtins = builtins // {
          getEnv = name: if name == "HOME" then legacyHome else "";
          pathExists = path: present && path == legacyPath;
        };
        import = path: if path == legacyPath then legacyValue else import path;
      } ../flake.nix;
      outputs = flake.outputs (inputs // {
        home-manager.lib.homeManagerConfiguration = args: {
          machineId = args.extraSpecialArgs.machineId;
          machineModule = builtins.elemAt args.modules 1;
        };
      });
    in outputs.homeConfigurations;

  missing = configurations false (throw "missing legacy config was imported");
  broken = configurations true (throw "legacy config points to missing ~/.dotfiles/config.nix");
  valid = configurations true {
    home.user = "legacy-user";
    home.dir = "/home/legacy-user";
    dotfiles.path = "/home/legacy-user/.dotfiles";
    git.name = "Legacy User";
    git.email = "legacy@example.invalid";
  };
  legacyMachine = valid.default.machineModule { };
  namedTargets = builtins.attrNames missing;
in
assert !(missing ? default);
assert namedTargets != [ ];
assert builtins.all
  (name:
    missing.${name}.machineId == name
    && broken.${name}.machineId == name
    && valid.${name}.machineId == name)
  namedTargets;
assert !(builtins.tryEval broken.default).success;
assert valid.default.machineId == "default";
assert legacyMachine.envy.user.name == "legacy-user";
assert legacyMachine.envy.user.home == "/home/legacy-user";
assert legacyMachine.envy.repository.path == "/home/legacy-user/.dotfiles";
assert legacyMachine.envy.git.email == "legacy@example.invalid";
true
