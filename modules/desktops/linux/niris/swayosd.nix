{ config, pkgs, lib, sys, ... }:

let
  enabled = config.envy.linux.option == "desktop"
    && (config.envy.linux.desktop == "niri" || config.envy.linux.desktop == "all")
    && !(builtins.elem "niri" config.envy.software.nix.packages.exclude);
  swayosdPkg = pkgs.swayosd;
in
{
  config = lib.mkIf enabled {
  home.activation.swayosdSystemSetup = sys.task.activation {
    after = [ "niriStartUp" ];
    name = "swayosd-system-setup";
    script = ''
      SWAYOSD_PATH="${swayosdPkg}"

      log_info "Deploying SwayOSD system configuration files..."

      # DBus configuration
      if [ -f "$SWAYOSD_PATH/share/dbus-1/system.d/org.erikreider.swayosd.conf" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/share/dbus-1/system.d/org.erikreider.swayosd.conf" \
          /etc/dbus-1/system.d/org.erikreider.swayosd.conf
        log_info "Installed DBus policy"
      fi

      if [ -f "$SWAYOSD_PATH/share/dbus-1/system-services/org.erikreider.swayosd.service" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/share/dbus-1/system-services/org.erikreider.swayosd.service" \
          /usr/share/dbus-1/system-services/org.erikreider.swayosd.service
        log_info "Installed DBus service"
      fi

      # Udev rules
      if [ -f "$SWAYOSD_PATH/lib/udev/rules.d/99-swayosd.rules" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/lib/udev/rules.d/99-swayosd.rules" \
          /etc/udev/rules.d/99-swayosd.rules
        log_info "Installed udev rules"
      fi

      # Systemd service
      if [ -f "$SWAYOSD_PATH/lib/systemd/system/swayosd-libinput-backend.service" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/lib/systemd/system/swayosd-libinput-backend.service" \
          /etc/systemd/system/swayosd-libinput-backend.service
        log_info "Installed systemd service"
      fi

      # Polkit policy
      if [ -f "$SWAYOSD_PATH/share/polkit-1/actions/org.erikreider.swayosd.policy" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/share/polkit-1/actions/org.erikreider.swayosd.policy" \
          /usr/share/polkit-1/actions/org.erikreider.swayosd.policy
        log_info "Installed polkit policy"
      fi

      if [ -f "$SWAYOSD_PATH/share/polkit-1/rules.d/org.erikreider.swayosd.rules" ]; then
        esudo ${sys.cmds.install} -D -m 0644 \
          "$SWAYOSD_PATH/share/polkit-1/rules.d/org.erikreider.swayosd.rules" \
          /etc/polkit-1/rules.d/org.erikreider.swayosd.rules
        log_info "Installed polkit rules"
      fi

      # Reload system services
      log_info "Reloading system services..."
      esudo_system ${sys.cmds.systemctl} daemon-reload
      esudo_system ${sys.cmds.systemctl} reload dbus || true
      esudo ${sys.cmds.udevadm} control --reload-rules
      esudo ${sys.cmds.udevadm} trigger

      # Enable and start the backend service
      esudo_system ${sys.cmds.systemctl} enable --now swayosd-libinput-backend.service || true

      log_info "SwayOSD system setup complete"
    '';
  };
  };
}
