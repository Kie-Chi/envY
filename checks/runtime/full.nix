{ pkgs }:

let
  base = pkgs.fetchurl {
    name = "ubuntu-24.04-minimal-cloudimg-amd64.img";
    url = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20261105/ubuntu-24.04-minimal-cloudimg-amd64.img";
    hash = "sha256-QGYwwIteVF/McgmFSxKdR+Ovgi2xKMnxkzZZO1otP+A=";
  };
  archive = pkgs.fetchurl {
    name = "nix-2.33.1-x86_64-linux.tar.xz";
    urls = [
      "https://releases.nixos.org/nix/nix-2.33.1/nix-2.33.1-x86_64-linux.tar.xz"
      "https://mirrors.tuna.tsinghua.edu.cn/nix/nix-2.33.1/nix-2.33.1-x86_64-linux.tar.xz"
    ];
    hash = "sha256-TMhgbc9cMVtAGXo5RFam3tu0ejQDAwtjI3FHDDTIc10=";
  };
  driver = pkgs.runCommand "envy-runtime-full-driver" { } ''
    mkdir "$out"
    cp ${./full.py} "$out/full.py"
    cp ${./acceptance.py} "$out/acceptance.py"
    cp ${./guest.py} "$out/guest.py"
    cp ${./cache.py} "$out/cache.py"
    cp ${./export.py} "$out/export.py"
    cp ${./snapshot.py} "$out/snapshot.py"
    cp ${./install.py} "$out/install.py"
    cp ${./vm.py} "$out/vm.py"
  '';
in
pkgs.writeShellApplication {
  name = "envy-runtime-full-probe";
  runtimeInputs = [ pkgs.qemu pkgs.cdrtools pkgs.python3 ]
    ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.ps ];
  text = ''
    ${pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
      export ENVY_RUNTIME_DARWIN_PS=${pkgs.ps}/bin/ps
    ''}
    export PYTHONDONTWRITEBYTECODE=1
    exec ${pkgs.python3}/bin/python3 ${driver}/full.py full-probe \
      --base ${base} \
      --sha256 406630c08b5e545fcc7209854b129d47e3af822db128c9f19336593b5a2d3fe0 \
      --nix-tar ${archive} \
      --qemu ${pkgs.qemu}/bin/qemu-system-x86_64 \
      --qemu-img ${pkgs.qemu}/bin/qemu-img \
      --mkisofs ${pkgs.cdrtools}/bin/mkisofs \
      ${pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin "--darwin-ps ${pkgs.ps}/bin/ps"} \
      "$@"
  '';
}
