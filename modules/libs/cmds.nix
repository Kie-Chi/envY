{ pkgs ? null }:

# Platform-aware system command paths.
# On macOS: use absolute paths to system binaries.
# On Linux: use absolute paths or nix-provided binaries.

let
  isDarwin = if pkgs != null then pkgs.stdenv.isDarwin else true;
in
if isDarwin then {
  sudo = "/usr/bin/sudo";
  sh = "/bin/sh";
  grep = "/usr/bin/grep";
  touch = "/usr/bin/touch";
  install = "/usr/bin/install";
  cmp = "/usr/bin/cmp";
  mktemp = "/usr/bin/mktemp";
  rm = "/bin/rm";
  cp = "/bin/cp";
  mkdir = "/bin/mkdir";
  cat = "/bin/cat";
  chown = "/usr/sbin/chown";
  pgrep = "/usr/bin/pgrep";
  pkill = "/usr/bin/pkill";
  killall = "/usr/bin/killall";
  launchctl = "/bin/launchctl";
  defaults = "/usr/bin/defaults";
  open = "/usr/bin/open";
  id = "/usr/bin/id";
  whoami = "/usr/bin/whoami";
  sw_vers = "/usr/bin/sw_vers";
  osascript = "/usr/bin/osascript";
  networksetup = "/usr/sbin/networksetup";
  route = "/sbin/route";
  awk = "/usr/bin/awk";
  sed = "/usr/bin/sed";
  head = "/usr/bin/head";
  ifconfig = "/sbin/ifconfig";
  scutil = "/usr/sbin/scutil";
} else {
  sudo = "/usr/bin/sudo";
  sh = "/bin/sh";
  grep = "${pkgs.gnugrep}/bin/grep";
  touch = "/usr/bin/touch";
  install = "/usr/bin/install";
  cmp = "/usr/bin/cmp";
  mktemp = "/usr/bin/mktemp";
  rm = "/usr/bin/rm";
  cp = "/usr/bin/cp";
  mkdir = "/usr/bin/mkdir";
  cat = "/usr/bin/cat";
  tee = "/usr/bin/tee";
  chown = "/usr/bin/chown";
  pgrep = "/usr/bin/pgrep";
  pkill = "/usr/bin/pkill";
  killall = "/usr/bin/killall";
  id = "/usr/bin/id";
  whoami = "/usr/bin/whoami";
  awk = "/usr/bin/awk";
  sed = "/usr/bin/sed";
  head = "/usr/bin/head";
  # Linux-specific
  apt = "/usr/bin/apt";
  systemctl = "/usr/bin/systemctl";
  usermod = "/usr/sbin/usermod";
  dpkg = "/usr/bin/dpkg";
  dpkgQuery = "/usr/bin/dpkg-query";
  udevadm = "/usr/bin/udevadm";
  modprobe = "/usr/sbin/modprobe";
  setcap = "/usr/sbin/setcap";
  curl = "${pkgs.curl}/bin/curl";
  ufw = "/usr/sbin/ufw";
  ifconfig = "/usr/sbin/ifconfig";
}
