{ config, lib, ... }:

let
  profile = (import ./resolve.nix { inherit lib; })
    (import ./catalog.nix).${config.envy.mirrors.mode}
    config.envy.mirrors.overrides;
  # Set upstream values explicitly too, so a first apply does not inherit
  # China bootstrap endpoints after selecting the upstream machine profile.
  homebrewEnv = {
    HOMEBREW_API_DOMAIN = profile.homebrew.apiDomain;
    HOMEBREW_BOTTLE_DOMAIN = profile.homebrew.bottleDomain;
    HOMEBREW_BREW_GIT_REMOTE = profile.homebrew.brewGitRemote;
    HOMEBREW_CORE_GIT_REMOTE = profile.homebrew.coreGitRemote;
    HOMEBREW_PIP_INDEX_URL = profile.python.index;
  };
in
{
  environment.variables = homebrewEnv;
  homebrew.onActivation.extraEnv = homebrewEnv;
}
