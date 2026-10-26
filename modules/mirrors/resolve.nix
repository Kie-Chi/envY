{ lib }:

profile: overrides:
let
  valueAt = path: fallback:
    let
      value = lib.attrByPath path null overrides;
    in
      if value == null || (builtins.isString value && value == "")
      then fallback
      else value;

  field = group: name: fallback:
    valueAt [ group name ] fallback;

in
profile // {
  nix = profile.nix // {
    substituters = field "nix" "substituters" profile.nix.substituters;
    extraSubstituters = field "nix" "extraSubstituters" profile.nix.extraSubstituters;
    extraTrustedPublicKeys = field "nix" "extraTrustedPublicKeys" profile.nix.extraTrustedPublicKeys;
  };
  npm = profile.npm // {
    registry = field "npm" "registry" profile.npm.registry;
  };
  python = profile.python // {
    index = field "python" "index" profile.python.index;
  };
  go = profile.go // {
    proxy = field "go" "proxy" profile.go.proxy;
  };
  rust = profile.rust // {
    distServer = field "rust" "distServer" profile.rust.distServer;
    updateRoot = field "rust" "updateRoot" profile.rust.updateRoot;
    cargoIndex = field "rust" "cargoIndex" profile.rust.cargoIndex;
  };
  maven = profile.maven // {
    repository = field "maven" "repository" profile.maven.repository;
  };
  conda = profile.conda // {
    defaultChannels = field "conda" "defaultChannels" profile.conda.defaultChannels;
    condaForge = field "conda" "condaForge" profile.conda.condaForge;
  };
  homebrew = profile.homebrew // {
    installerUrl = field "homebrew" "installerUrl" profile.homebrew.installerUrl;
    apiDomain = field "homebrew" "apiDomain" profile.homebrew.apiDomain;
    artifactDomain = field "homebrew" "artifactDomain" profile.homebrew.artifactDomain;
    bottleDomain = field "homebrew" "bottleDomain" profile.homebrew.bottleDomain;
    brewGitRemote = field "homebrew" "brewGitRemote" profile.homebrew.brewGitRemote;
    coreGitRemote = field "homebrew" "coreGitRemote" profile.homebrew.coreGitRemote;
  };
  apt = profile.apt // {
    ubuntu = field "apt" "ubuntu" profile.apt.ubuntu;
    ubuntuPorts = field "apt" "ubuntuPorts" profile.apt.ubuntuPorts;
    debian = field "apt" "debian" profile.apt.debian;
    debianSecurity = field "apt" "debianSecurity" profile.apt.debianSecurity;
  };
  docker = profile.docker // {
    registry = field "docker" "registry" profile.docker.registry;
  };
  flakeSourceMirror = valueAt [ "flakeSourceMirror" ] (profile.flakeSourceMirror or null);
  dockerInstallerMirror = valueAt [ "dockerInstallerMirror" ] profile.dockerInstallerMirror;
}
