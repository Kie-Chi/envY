{ pkgs, lib, config, ... }:

let
  cmds = import ./cmds.nix { inherit pkgs; };
  linuxPackages = lib.optionalAttrs (!pkgs.stdenv.isDarwin)
    (import ./linux-packages.nix { inherit pkgs cmds; });
  activation = import ./activation.nix { inherit config lib pkgs cmds linuxPackages; };
  deploy = import ./deploy.nix { inherit lib pkgs cmds; inherit (activation) task; };
in
{
  # Helpers shared by feature modules through the `sys` module argument.
  _module.args.sys = {
    inherit cmds;
    inherit (activation) task;
    inherit (deploy) deploy mergeWritable;
    config = deploy;
  };
  home.activation = {
    sysInit = activation.initActivation;
    inherit (activation) sopsDecrypt userBoundary;
  };
}
