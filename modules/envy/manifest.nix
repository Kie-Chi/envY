{ lib }:

let
  packageItem = selection: package:
    let
      name = lib.getName package;
      version = lib.getVersion package;
    in {
      id = name;
      inherit name;
      version = null;
      resolvedVersion = if version == "" then null else version;
      ref = selection.references.${name} or null;
      sourceId = selection.sourceIds.${name} or (
        let ref = selection.references.${name} or "";
        in if lib.hasPrefix "nix:" ref then "nix:nixpkgs"
        else if lib.hasPrefix "flake:" ref then "nix:" + builtins.head (lib.splitString "#" (lib.removePrefix "flake:" ref))
        else if lib.hasPrefix "local:" ref then "nix:envy-local" else null
      );
      parameters = lib.optionalAttrs (selection.sourceDependencies ? ${name}) { sourceDependencies = selection.sourceDependencies.${name}; };
    };

  stringItem = refPrefix: name: {
    id = name;
    inherit name;
    version = null;
    ref = "${refPrefix}/${name}";
    parameters = { };
  };

  normalize = convert: selection: {
    include = map convert selection.include;
    exclude = selection.exclude;
    effective = map convert selection.effective;
  };
in
{
  packageSelection = selection: {
    include = map (packageItem selection) selection.include;
    inherit (selection) exclude;
    effective = map (packageItem selection) selection.effective;
  };
  itemSelection = selection: {
    inherit (selection) include exclude effective;
  };
  stringSelection = refPrefix: normalize (stringItem refPrefix);

  group = {
    label,
    optionPath,
    ecosystem,
    platforms,
    scope,
    kind,
    installer,
    selection,
    editableInclude ? false,
    editableExclude ? true,
    reconcileInstall ? true,
    reconcileUpgrade ? false,
    reconcileRemove ? false,
  }: {
    inherit label optionPath ecosystem platforms scope kind installer selection;
    editable = {
      include = editableInclude;
      exclude = editableExclude;
    };
    reconcile = {
      install = reconcileInstall;
      upgrade = reconcileUpgrade;
      remove = reconcileRemove;
    };
  };
}
