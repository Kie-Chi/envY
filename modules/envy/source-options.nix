{ lib, ... }@moduleArgs:
let
  bindings = moduleArgs.softwareSourceBindings or { };
  allowed = {
    flake = [ "input" "output" "follows" ];
    local = [ "path" ];
    native = [ "apt" "dnf" "pacman" "zypper" "distros" "system" ];
    tap = [ "tap" "url" ];
    registry = [ "registry" "index" "metadata" "discoveryOnly" ];
    artifact = [ "url" "format" "packageName" "version" "architecture" "hash" "bootstrapRepository" ];
  };
  compatible = {
    nix = [ "flake" "local" ]; native = [ "native" ]; homebrew = [ "tap" ];
    npm = [ "registry" ]; pypi = [ "registry" ]; cargo = [ "registry" ];
    go = [ "registry" ]; url = [ "artifact" ];
  };
  publicUrl = value: builtins.isString value
    && builtins.match "https://[^@[:space:]]+" value != null
    && !(lib.hasInfix "#" value)
    && builtins.match ".*[?&][^=&]*(token|secret|password|api.?key|signature|authorization|credential)[^=&]*=.*" (lib.toLower value) == null
    && builtins.match ".*[?&]sig=.*" (lib.toLower value) == null;
  nonEmptyString = value: builtins.isString value && value != "";
  validList = values: builtins.isList values && lib.all (value:
    builtins.isString value && builtins.match "[A-Za-z0-9_{}+./-]+" value != null
  ) values;
  validHash = hash: builtins.isString hash && (
    builtins.match "(sha256:)?[A-Fa-f0-9]{64}" hash != null
    || builtins.match "sha256-[A-Za-z0-9+/]{43}=" hash != null
  );
  logicalRegistries = {
    "npm:npmjs" = { registry = "https://registry.npmjs.org"; };
    "pypi:pypi" = { index = "https://pypi.org/simple"; metadata = "https://pypi.org/pypi"; };
    "cargo:crates.io" = { metadata = "https://crates.io"; };
    "go:pkg.go.dev" = { metadata = "https://pkg.go.dev"; };
  };
  valid = id: source:
    let parameters = source.parameters;
        validNative = manager:
          let spec = parameters.${manager} or null;
          in spec == null || (builtins.isAttrs spec
            && lib.all (key: builtins.elem key [
              "uri" "suites" "components" "architectures" "keyUrl" "keyFingerprint" "server" "sigLevel"
            ]) (builtins.attrNames spec)
            && publicUrl (spec.uri or (spec.server or ""))
            && lib.all (key: !(spec ? ${key}) || publicUrl spec.${key}) [ "uri" "keyUrl" "server" ]
            && lib.all (key: validList (spec.${key} or [ ])) [ "suites" "components" "architectures" ]
            && (manager != "apt" || (spec.suites or [ ]) != [ ])
            && (manager == "pacman" || (
              publicUrl (spec.keyUrl or "")
              && builtins.match "([A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})" (spec.keyFingerprint or "") != null
            )));
    in builtins.match "[a-z][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._/-]*" id != null
      && lib.hasPrefix (source.ecosystem + ":") id
      && builtins.elem source.type compatible.${source.ecosystem}
      && lib.all (key: builtins.elem key allowed.${source.type}) (builtins.attrNames parameters)
      && lib.all (key: !(builtins.hasAttr key parameters) || publicUrl parameters.${key}) [ "url" "registry" "index" "metadata" ]
      && (!(builtins.hasAttr id logicalRegistries) || lib.all (key:
        lib.removeSuffix "/" (parameters.${key} or "") == lib.removeSuffix "/" logicalRegistries.${id}.${key}
      ) (builtins.attrNames logicalRegistries.${id}))
      && (source.type != "native" || (
        lib.all validNative [ "apt" "dnf" "pacman" "zypper" ]
        && (!(parameters ? system) || builtins.isBool parameters.system)
        && validList (parameters.distros or [ ])
      ))
      && (source.type != "tap" || (
        builtins.isString (parameters.tap or null)
        && builtins.match "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" parameters.tap != null
        && id == "homebrew:" + parameters.tap
      ))
      && (source.type != "local" || nonEmptyString (parameters.path or null))
      && (source.type != "registry" || (
        publicUrl (parameters.${if source.ecosystem == "npm" then "registry" else if source.ecosystem == "pypi" then "index" else "metadata"} or null)
        && (!(parameters ? discoveryOnly) || builtins.isBool parameters.discoveryOnly)
      ))
      && (source.type != "artifact" || (
        publicUrl (parameters.url or null)
        && builtins.elem (parameters.format or null) [ "deb" "rpm" ]
        && builtins.isString (parameters.packageName or null)
        && builtins.match "[A-Za-z0-9][A-Za-z0-9+_.-]*" parameters.packageName != null
        && validHash (parameters.hash or null)
        && lib.all (key: !(parameters ? ${key}) || (
          nonEmptyString parameters.${key} && builtins.match ".*[[:space:]].*" parameters.${key} == null
        )) [ "version" "architecture" "bootstrapRepository" ]
      ))
      && (source.type != "flake" || (
        builtins.hasAttr (parameters.input or "") bindings
        && id == "nix:" + parameters.input
        && parameters == bindings.${parameters.input}
      ));
in {
  options.envy.software.sources = lib.mkOption {
    default = { };
    description = "Software source definitions; selection remains in ecosystem groups.";
    apply = sources:
      assert lib.assertMsg (lib.all (id: valid id sources.${id}) (builtins.attrNames sources))
        "Software source parameters or locked input binding are invalid";
      sources;
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        label = lib.mkOption { type = lib.types.str; };
        ecosystem = lib.mkOption { type = lib.types.enum [ "nix" "native" "homebrew" "npm" "pypi" "cargo" "go" "url" ]; };
        type = lib.mkOption { type = lib.types.enum [ "flake" "local" "native" "tap" "registry" "artifact" ]; };
        platforms = lib.mkOption { type = lib.types.listOf (lib.types.enum [ "darwin" "linux" ]); default = [ "darwin" "linux" ]; };
        scope = lib.mkOption { type = lib.types.enum [ "user" "system" "project" ]; default = "user"; };
        owner = lib.mkOption { type = lib.types.str; default = "module"; };
        parameters = lib.mkOption { type = lib.types.attrsOf lib.types.anything; default = { }; };
      };
    });
  };
}
