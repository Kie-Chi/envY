{ config, lib, ... }:

{
  imports = [
    ./modules/envy/home.nix
    ./modules/software
    ./modules/mirrors
    ./modules/llm
    ./modules/storage
    ./modules/agents
    ./modules/cores
    ./modules/devps
    ./modules/desktops
    ./modules/libs
  ];
  config = {
    home.username = config.envy.user.name;
    home.homeDirectory = lib.mkForce config.envy.user.home;
    home.stateVersion = "25.11";

    programs.home-manager.enable = true;
  };
}
