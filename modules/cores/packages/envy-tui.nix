{ lib, rustPlatform }:

# An in-tree Cargo target directory slows path: flake evaluation and is
# copied into both this source and the bundled envy scripts.
lib.warnIf (builtins.pathExists ../../../resources/scripts/envy-tui/target)
  "envY: resources/scripts/envy-tui/target is inside the checkout; remove it and build with CARGO_TARGET_DIR outside the repository (the devShell and ./envy set it)"
  (rustPlatform.buildRustPackage {
    pname = "envy-tui";
    version = "0.1.0";
    src = ../../../resources/scripts/envy-tui;
    cargoLock.lockFile = ../../../resources/scripts/envy-tui/Cargo.lock;
    doCheck = true;
    meta = {
      description = "Ratatui frontend for the envY configuration manager";
      mainProgram = "envy-tui";
    };
  })
