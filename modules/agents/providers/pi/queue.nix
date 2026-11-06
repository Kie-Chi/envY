{ lib, pkgs, piPackage }:

let
  fetchPiExtension = import ./npm-extension.nix { inherit pkgs; };
in
fetchPiExtension {
  pname = "pi-queue-steer-factory";
  # This release is verified upstream with Pi 1.0; envY locks Pi 1.1.
  version = "0.17.8";
  url = "https://registry.npmjs.org/pi-queue-steer-factory/-/pi-queue-steer-factory-0.17.8.tgz";
  hash = "sha512-VymX2fEg+aOSxsQwM2KIPtCxnu9zCjfAJR0eU0ZAwbmviypQ159UHAVxt6lTQDp85G/pr5HgNBE7Ox/ydYqxQA==";
  postUnpack = ''
    ${lib.getExe pkgs.nodejs_24} ${./tests/pi-queue.mjs} \
      "$out" ${piPackage}/lib/pi/node_modules
  '';
}
