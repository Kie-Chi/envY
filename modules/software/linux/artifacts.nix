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
