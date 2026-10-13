{ lib }:

let
  inherit (lib) mkOption types;
  softwareItem = types.submodule {
    options = {
      id = mkOption {
        type = types.nonEmptyStr;
        description = "Stable identifier used by machine exclusions.";
      };
      name = mkOption {
        type = types.nonEmptyStr;
        description = "Package name in the source ecosystem.";
      };
      version = mkOption {
        type = types.nullOr types.nonEmptyStr;
        default = null;
        description = "Pinned package version, when one is declared.";
      };
      ref = mkOption {
        type = types.nullOr types.nonEmptyStr;
        default = null;
        description = "Canonical envY software reference, when available.";
      };
      sourceId = mkOption {
        type = types.nullOr types.nonEmptyStr;
        default = null;
        description = "Stable identity of the declared software source.";
      };
      parameters = mkOption {
        type = types.attrsOf types.anything;
        default = { };
        description = "Structured installer-specific parameters.";
      };
    };
  };
in
{
  inherit softwareItem;

  packageSelection = description: {
    include = mkOption {
      type = types.listOf types.package;
      default = [ ];
      inherit description;
    };
    sourceDependencies = mkOption {
      type = types.attrsOf (types.listOf types.nonEmptyStr);
      default = { };
      description = "Upstream software sources used by local wrapper derivations.";
    };
    sourceIds = mkOption {
      type = types.attrsOf types.nonEmptyStr;
      default = { };
      description = "Source identities keyed by final package name.";
    };
    references = mkOption {
      type = types.attrsOf types.nonEmptyStr;
      default = { };
      description = ''
        Canonical source references keyed by the final package name. Use
        `nix:<attr-path>` for a nixpkgs attribute, `flake:<input>#<attr>` for
        an external flake, and `local:<path>` for a derivation owned by this
        repository. References are metadata only; package installation
        continues to be owned by `include`.
      '';
    };
    exclude = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Package names removed from ${description}; exclusions win over inclusions.";
    };
    effective = mkOption {
      type = types.listOf types.package;
      readOnly = true;
      description = "Final ${description} after de-duplication and exclusions.";
    };
  };

  stringSelection = description: {
    include = mkOption {
      type = types.listOf types.str;
      default = [ ];
      inherit description;
    };
    exclude = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Entries removed from ${description}; exclusions win over inclusions.";
    };
    effective = mkOption {
      type = types.listOf types.str;
      readOnly = true;
      description = "Final ${description} after de-duplication and exclusions.";
    };
  };

  itemSelection = description: {
    include = mkOption {
      type = types.listOf softwareItem;
      default = [ ];
      inherit description;
    };
    exclude = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Stable item IDs removed from ${description}; exclusions win over inclusions.";
    };
    effective = mkOption {
      type = types.listOf softwareItem;
      readOnly = true;
      description = "Final ${description} after de-duplication and exclusions.";
    };
  };
}
