{ ... }:

{
  # Home Manager implementations for Darwin desktop features. System-level
  # desktop policy is composed separately by nix-darwin.nix.
  imports = [
    ./app-management.nix
    ./sing-box.nix
    ./karabiner.nix
    ./linearmouse.nix
    ./dmgs.nix
    ./zips.nix
    ./clash-verge.nix
    ./proxies.nix
    ./wallpaper.nix
    ./squirrel.nix
    ./raycast-ai.nix
    ./zotero.nix
  ];
}
