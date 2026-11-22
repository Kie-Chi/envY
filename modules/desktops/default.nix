{ lib, machinePlatform, ... }:

{
  # Desktops is the public feature entry point. Platform selection remains an
  # implementation detail below this directory.
  imports =
    [ ./terminal.nix ]
    ++ lib.optionals (machinePlatform == "darwin") [ ./darwin ]
    ++ lib.optionals (machinePlatform == "linux") [ ./linux ];
}
