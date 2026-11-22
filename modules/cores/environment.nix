{ config, pkgs, lib, ... }:

let
  selectedPackages = map lib.getName config.envy.software.nix.packages.effective;
  direnvSelected = builtins.elem "direnv" selectedPackages;
  nixDirenvSelected = direnvSelected && builtins.elem "nix-direnv" selectedPackages;
in
{
  envy.software.nix.packages.include = [ pkgs.direnv pkgs.nix-direnv ];
  envy.software.nix.packages.references = {
    direnv = "nix:direnv";
    "nix-direnv" = "nix:nix-direnv";
  };

  # Keep installation in envY; the locked Home Manager module always installs direnv.
  programs.direnv.enable = false;
  programs.zsh.initContent = lib.mkIf direnvSelected (lib.mkOrder 900 ''
    eval "$(${lib.getExe pkgs.direnv} hook zsh)"
  '');
  xdg.configFile."direnv/lib/nix-direnv.sh" = lib.mkIf nixDirenvSelected {
    source = "${pkgs.nix-direnv}/share/nix-direnv/direnvrc";
  };
}
