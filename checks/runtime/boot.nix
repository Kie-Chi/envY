{ pkgs }:

let
  base = pkgs.fetchurl {
    name = "ubuntu-24.04-minimal-cloudimg-amd64.img";
    url = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20261105/ubuntu-24.04-minimal-cloudimg-amd64.img";
    hash = "sha256-QGYwwIteVF/McgmFSxKdR+Ovgi2xKMnxkzZZO1otP+A=";
  };
in
pkgs.writeShellApplication {
  name = "envy-runtime-boot-probe";
  runtimeInputs = [
    pkgs.qemu
    pkgs.cdrtools
    pkgs.python3
  ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.ps ];
  text = ''
    ${pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
      export ENVY_RUNTIME_DARWIN_PS=${pkgs.ps}/bin/ps
    ''}
    exec ${pkgs.python3}/bin/python3 ${./vm.py} boot-probe \
      --base ${base} \
      --sha256 406630c08b5e545fcc7209854b129d47e3af822db128c9f19336593b5a2d3fe0 \
      --qemu ${pkgs.qemu}/bin/qemu-system-x86_64 \
      --qemu-img ${pkgs.qemu}/bin/qemu-img \
      --mkisofs ${pkgs.cdrtools}/bin/mkisofs \
      ${pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin "--darwin-ps ${pkgs.ps}/bin/ps"} \
      "$@"
  '';
}
