{ config, lib, pkgs, sys, ... }:

let
  sourceDefinitions = pkgs.writeText "envy-artifact-sources.json" (builtins.toJSON config.envy.software.sources);
  artifacts = config.envy.linux.software.url.artifacts.effective;
  installArtifact = artifact:
    let
      parameters = artifact.parameters // { packageName = artifact.parameters.packageName or artifact.name; }
        // lib.optionalAttrs (artifact.version != null) { version = artifact.version; };
      format = parameters.format or "";
      descriptor = pkgs.writeText ("envy-artifact-" + artifact.id + ".json") (builtins.toJSON parameters);
    in if !(builtins.elem format [ "deb" "rpm" ]) then throw "Unsupported artifact format"
    else ''
      case "$(detect_pkg_manager):${format}" in
        apt:deb|dnf:rpm|zypper:rpm)
          PKG=${lib.escapeShellArg parameters.packageName}
          if ! pkg_installed "$PKG"; then
            TARGET="$TEMP_DIR/${artifact.id}.${format}"
            ${pkgs.python3}/bin/python3 -c 'import sys; sys.path.insert(0, "${../../../resources/scripts}"); from envy.sources.artifacts import main; main()' "${descriptor}" "$TARGET"
            ${pkgs.python3}/bin/python3 -c 'import sys,json; sys.path.insert(0, "${../../../resources/scripts}"); from envy.sources.native.observe import snapshot; json.dump(snapshot(),open(sys.argv[1],"w"))' "$TEMP_DIR/before-sources.json"
            pkg_install_files "$TARGET"
            ${pkgs.python3}/bin/python3 -c 'import sys,json; sys.path.insert(0, "${../../../resources/scripts}"); from envy.sources.native.observe import report_bootstrap; report_bootstrap(json.load(open(sys.argv[1])),sys.argv[2],json.load(open(sys.argv[3])))' "${descriptor}" "$TEMP_DIR/before-sources.json" "${sourceDefinitions}"
            log_info "Artifact installed; native source configuration will be observed on the next source/doctor inspection."
          fi
          ;;
        *) log_warn "Artifact ${artifact.id} is unsupported by this package manager." ;;
      esac
    '';

in
{
  envy.machine.activationActions = lib.optional (artifacts != []) {
    id = "native-artifacts";
    owner = "modules/software/linux/artifacts.nix";
    manager = "native-artifact";
    summary = "Ensure ${toString (builtins.length artifacts)} selected system artifacts are installed on a compatible Linux package manager.";
    effects = [ "Download and verify missing selected deb/rpm artifacts, install them with elevated privileges, and observe repository bootstrap changes; package scripts may run." ];
    verification = "Inspect selected artifact metadata and installed package state; metadata inspection does not download artifacts or invoke the native installer.";
    repeatability = "Installed package names are skipped and incompatible manager/format pairs are skipped; activation does not reconcile existing versions. This description does not authorize a retry.";
    rollbackBoundary = "A generation rollback does not uninstall artifacts, restore native package state, or undo repository bootstrap, package scripts, and service changes.";
  };
  home.activation.installSystemArtifacts = sys.task.activation {
    name = "system-artifacts";
    after = [ "configureNativeRepositories" ];
    script = ''
      TEMP_DIR=$(mktemp -d)
      trap 'rm -rf "$TEMP_DIR"' EXIT
      ${lib.concatMapStringsSep "\n" installArtifact artifacts}
    '';
  };
}
