{ config, lib, pkgs, piPackage, ... }:

let
  selectedPiPackage = import ./package.nix {
    inherit lib piPackage;
    inherit (config.envy.agents.pi) moreThinking;
  };
  files = config.home.file;
  entriesUnder = prefix: lib.mapAttrsToList (target: file: {
    name = lib.removePrefix prefix target;
    inherit target;
    recursive = file.recursive or false;
    source = "${file.source}";
    version = if lib.isDerivation file.source then lib.getVersion file.source else null;
  }) (lib.filterAttrs (target: _: lib.hasPrefix prefix target
    && !(lib.hasInfix "/" (lib.removePrefix prefix target))) files);
  extensions = lib.filter (entry: lib.hasPrefix "envy-" entry.name)
    (entriesUnder ".pi/agent/extensions/");
  roles = entriesUnder ".pi/agent/agents/";
  skills = entriesUnder ".pi/agent/skills/";
  mcpServers = builtins.foldl' (servers: runtime:
    servers // lib.mapAttrs (_: server: {
      enabled = server.enabled or true;
      command = server.command or "";
      args = server.args or [ ];
    }) (runtime.data.mcpServers or { })
  ) { } config.envy.agents.pi.runtimeFiles;
in
{
  # Describe only envY-owned resources. Runtime state, credentials, and user
  # extensions stay application-owned and are never copied into this contract.
  home.file.".pi/agent/envy-runtime.json".text = builtins.toJSON {
    schemaVersion = 1;
    pi = {
      enabled = !(builtins.elem "pi" config.envy.software.nix.packages.exclude);
      version = lib.getVersion selectedPiPackage;
      node = lib.getExe pkgs.nodejs_24;
      nodeModules = "${selectedPiPackage}/lib/pi/node_modules";
      probe = "${../../../../files/agents/pi}/doctor.mjs";
    };
    inherit extensions roles skills mcpServers;
  };
}
