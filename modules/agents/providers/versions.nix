{ pkgs, ... }:

let
  lock = builtins.fromJSON (builtins.readFile ./agent-lock.json);
  # The lock is also embedded in Darwin amux configurations, whose containers
  # run Linux on the host CPU.
  platform =
    if pkgs.stdenv.hostPlatform.isAarch64 then "linux-arm64"
    else if pkgs.stdenv.hostPlatform.isx86_64 then "linux-x64"
    else throw "envY agent packages support only x86_64 and aarch64 CPUs";
  specFor = name: lock.agents.${name}.platforms.${platform};
in {
  inherit lock platform;
  claude = specFor "claude";
  codex = specFor "codex";
}
