{ ... }:

{
  # Home Manager implementations for Darwin-only agent features. The
  # system-level contribution is composed separately by nix-darwin.nix.
  imports = [
    ./cmux.nix
    ./pi-cron.nix
  ];
}
