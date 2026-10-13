{ config, pkgs, lib, sys, ... }:

let
  wallpaperSource = ../../../resources/images/background.jpg;
  # macOS keeps the wallpaper URL in WallpaperAgent's store.  Keep that URL
  # outside the Nix store so a Home Manager generation switch cannot make it
  # temporarily disappear or leave WallpaperAgent with a stale symlink.
  wallpaperDir = "${config.home.homeDirectory}/Library/Application Support/envy/wallpapers";
  wallpaperPath = "${wallpaperDir}/background.jpg";
in

{
  envy.software.nix.packages.include =
    [ pkgs.desktoppr ];
  envy.software.nix.packages.references.desktoppr = "nix:desktoppr";

  home.file.".config/wallpapers/background.jpg" =
    lib.mkIf (builtins.elem "desktoppr" (map lib.getName config.envy.software.nix.packages.effective)) {
      # Keep the legacy convenience path, but make it point at the persistent
      # copy rather than at a generation-specific Nix store path.
      source = config.lib.file.mkOutOfStoreSymlink wallpaperPath;
    };

  home.activation.deployWallPaper = lib.mkIf (
    builtins.elem "desktoppr" (map lib.getName config.envy.software.nix.packages.effective)
  ) (sys.task.activation {
    name = "deployWallPaper";
    script = sys.deployScript {
      source = wallpaperSource;
      target = wallpaperPath;
      owner = config.home.username;
      group = "staff";
      mode = "0644";
      useSudo = false;
    };
  });

  home.activation.setWallPaper = lib.mkIf (
    builtins.elem "desktoppr" (map lib.getName config.envy.software.nix.packages.effective)
  ) (sys.task.activation {
    after = [ "deployWallPaper" ];
    name = "setWallPaper";
    script = ''
      WALLPAPER_PATH=${lib.escapeShellArg wallpaperPath}
      if [ ! -r "$WALLPAPER_PATH" ]; then
        log_warn "Wallpaper copy is not readable; leaving the current desktop picture unchanged"
        exit 0
      fi

      echo "Setting desktop wallpaper with macOS System Events..."
      if ! ${sys.cmds.osascript} -e ${lib.escapeShellArg "tell application \"System Events\" to tell every desktop to set picture to POSIX file \"${wallpaperPath}\""}; then
        log_warn "System Events could not set the wallpaper; trying desktoppr"
        ${pkgs.desktoppr}/bin/desktoppr "$WALLPAPER_PATH"
      fi
    '';
  });
}
