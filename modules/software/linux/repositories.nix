{ config, lib, pkgs, sys, ... }:
let
  repositories = config.envy.linux.software.native.repositories;
  sources = config.envy.software.sources;
  selected = map (item: if item.sourceId == null then "native:" + item.id else item.sourceId) repositories.effective;
  definitions = pkgs.writeText "envy-native-sources.json" (builtins.toJSON { inherit sources selected; });
  runtime = pkgs.writeShellApplication {
    name = "envy-native-sources";
    runtimeInputs = [ pkgs.python3 pkgs.gnupg ];
    text = ''
      exec python3 -c 'import sys; sys.path.insert(0, "${../../../resources/scripts}"); from envy.sources.native.backend import main; main()' activate --definitions "${definitions}"
    '';
  };
in {
  envy.machine.activationActions = [ {
    id = "native-repositories";
    owner = "modules/software/linux/repositories.nix";
    manager = "native-repository";
    summary = "Reconcile ${toString (builtins.length selected)} selected native repositories and remove stale envY-owned repository declarations, including when the selection is empty.";
    effects = [ "On supported Linux managers, verify selected repository signing keys, write envY-owned repository/keyring files, and remove deselected envY-owned repository files; retain external declarations." ];
    verification = "Inspect effective source selection and envY-owned repository files; metadata inspection does not fetch keys, write files, or run repository activation.";
    repeatability = "The backend reconciles owned repository files with the effective selection, subject to host manager support and network/key verification. This description does not authorize a retry.";
    rollbackBoundary = "A generation rollback does not immediately restore repository or keyring files; later activation may reconcile repository declarations but cannot undo packages already installed from them.";
  } ];
  assertions = [{
    assertion = lib.all (id: builtins.hasAttr id sources) selected;
    message = "Every selected native repository must have a source definition";
  } {
    assertion = lib.all (item: item.sourceId == null || item.sourceId == "native:system" ||
      builtins.elem item.sourceId selected)
      config.envy.linux.software.native.packages.effective;
    message = "Native packages require their source in the effective repository selection";
  }];
  home.activation.configureNativeRepositories = sys.task.activation {
    name = "native-repositories";
    after = [ "configureAptMirror" ];
    script = ''esudo_system ${runtime}/bin/envy-native-sources'';
  };
}
