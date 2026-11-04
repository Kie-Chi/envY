{ pkgs, config, sys, lib, nixGLDefault ? null, ... }:

let
  isDesktop = config.envy.linux.option == "desktop";
  waydroidEnabled = isDesktop && !(builtins.elem "waydroid" config.envy.software.nix.packages.exclude);
  xresourceDesktop = pkgs.runCommand "xresource-desktop" {} ''
    mkdir -p $out/share/applications
    cp ${../../../files/desktop/xresource.desktop} $out/share/applications/xresource.desktop
  '';
  waydroidHelper = pkgs.writeShellScriptBin "waydroid-helper" ''
    exec ${pkgs.steam-run}/bin/steam-run ${pkgs.waydroid-helper}/bin/waydroid-helper "$@"
  '';
in
{
  config = lib.mkIf isDesktop {
  envy.software.sources."native:waydroid" = {
    label = "Waydroid official repository"; ecosystem = "native"; type = "native";
    scope = "system"; platforms = [ "linux" ]; owner = "modules/desktops/linux/base.nix";
    parameters = {
      distros = [ "ubuntu" "debian" ];
      apt = {
        uri = "https://repo.waydro.id/";
        suites = [ "{codename}" ]; components = [ "main" ];
        keyUrl = "https://repo.waydro.id/waydroid.gpg";
        keyFingerprint = "7CE0331F71E0A238BB1002D70E406D181DCEE19C";
      };
    };
  };
  envy.linux.software.native.repositories.include = lib.optionals waydroidEnabled [
    { id = "waydroid"; name = "waydroid"; ref = "native:waydroid"; sourceId = "native:waydroid"; }
  ];
  envy.linux.software.native.packages.include = lib.optionals waydroidEnabled [
    { id = "waydroid"; name = "waydroid"; ref = "native:waydroid"; sourceId = "native:waydroid"; }
  ];
  envy.software.nix.packages.include = (with pkgs; [
    # utils
    xclip
    xsel
    copyq
    slurp
    grim
    swappy
    wev

    # apps
    steam-run
    waydroid-nftables
    kdePackages.okular
    pavucontrol
    (pkgs.feishu.overrideAttrs (oldAttrs: {
      postFixup = (oldAttrs.postFixup or "") + ''
        wrapProgram $out/bin/bytedance-feishu \
          --add-flags "--no-sandbox" \
          --add-flags "--disable-gpu-sandbox" \
          --prefix PATH : ${pkgs.lib.makeBinPath [
            pkgs.xdg-utils
            pkgs.google-chrome
          ]}
      '';
    }))
    wemeet
    todesk
    google-chrome
    wpsoffice-cn
    remmina

    # patches
    libcanberra-gtk3
    mesa
  ])
  ++ lib.optionals waydroidEnabled [ waydroidHelper ]
  ++ lib.optionals (nixGLDefault != null) [ nixGLDefault ];

  envy.software.nix.packages.references = {
    xclip = "nix:xclip";
    xsel = "nix:xsel";
    CopyQ = "nix:copyq";
    slurp = "nix:slurp";
    grim = "nix:grim";
    swappy = "nix:swappy";
    wev = "nix:wev";
    "steam-run" = "nix:steam-run";
    waydroid = "nix:waydroid-nftables";
    okular = "nix:kdePackages.okular";
    pavucontrol = "nix:pavucontrol";
    feishu = "nix:feishu";
    wemeet = "nix:wemeet";
    todesk = "nix:todesk";
    "google-chrome" = "nix:google-chrome";
    "wpsoffice-cn" = "nix:wpsoffice-cn";
    remmina = "nix:remmina";
    libcanberra = "nix:libcanberra-gtk3";
    mesa = "nix:mesa";
  } // lib.optionalAttrs waydroidEnabled {
    "waydroid-helper" = "local:modules/desktops/linux/base.nix#waydroidHelper";
  } // lib.optionalAttrs (nixGLDefault != null) (
    lib.setAttrByPath [ (lib.getName nixGLDefault) ] "local:flake.nix#nixGLDefault"
  );

  home.pointerCursor = {
    name = "Yaru";
    package = pkgs.yaru-theme;
    size = 24;
    x11.enable = true;
    gtk.enable = true;
  };

  home.sessionVariables.XDG_DATA_DIRS = "$GSETTINGS_SCHEMAS_PATH:$XDG_DATA_DIRS";

  xresources.properties = {
    # 144 (1.5x), 168 (1.75x), 192 (2x)
    "Xft.dpi" = 144;
  };
  xsession.enable = true;

  systemd.user.services.xresources-wayland = {
    Unit = {
      Description = "Load Xresources for XWayland";
      PartOf = [ "graphical-session.target" ];
    };
    Install = {
      WantedBy = [ "graphical-session.target" ];
    };
    Service = {
      Type = "oneshot";
      ExecStart = "/usr/bin/xrdb -merge %h/.Xresources";
    };
  };

  xdg.autostart.enable = true;
  xdg.autostart.entries = [
    xresourceDesktop
  ];
  xdg.desktopEntries = {
    wechat = {
      name = "WeChat";
      comment = "WeChat Desktop App";
      exec = "/usr/bin/wechat";
      icon = "wechat";
      terminal = false;
      categories = [ "Utility" ];
    };
  };

  xdg.desktopEntries."id.waydro.waydroid_helper" = lib.mkIf waydroidEnabled {
    name = "Waydroid Helper";
    exec = "waydroid-helper";
    icon = "waydroid";
    terminal = false;
    categories = [ "System" ];
  };

  home.activation.installWayDroid = lib.mkIf waydroidEnabled (sys.task.activation {
    name = "installWayDroid";
    after = [ "installNativePackages" ];
    script = ''
      if pkg_installed "waydroid"; then
        WAYDROID_BIN=""
        if [ -x /usr/bin/waydroid ]; then
          WAYDROID_BIN=/usr/bin/waydroid
        elif WAYDROID_BIN="$(command -v waydroid 2>/dev/null)" && [ -x "$WAYDROID_BIN" ]; then
          :
        else
          WAYDROID_BIN=""
        fi

        if [ -n "$WAYDROID_BIN" ]; then
          "$WAYDROID_BIN" prop set persist.waydroid.multi_windows true
          esudo_system ${sys.cmds.systemctl} restart waydroid-container
        else
          log_warn "Waydroid package is installed, but the waydroid command is unavailable; skipping initialization."
        fi
      fi
    '';
  });

  };
}
