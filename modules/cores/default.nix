{ ... }:

{
  imports = [
    ./secrets.nix
    ./base.nix
    ./shell.nix
    ./tmux.nix
    ./git.nix
    ./ssh.nix
    ./util.nix
  ];
}
