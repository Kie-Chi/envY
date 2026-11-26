{ ... }:

{
  imports = [
    ./secrets.nix
    ./base.nix
    ./shell.nix
    ./environment.nix
    ./files.nix
    ./tmux.nix
    ./git.nix
    ./ssh.nix
    ./util.nix
  ];
}
