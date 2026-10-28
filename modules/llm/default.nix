{ config, lib, ... }:

{
  imports = [ ./options.nix ];

  # Every logical credential becomes one secret named llm-credential-<name>;
  # agents, Raycast, and Zotero all read keys through these names.
  sops.secrets = lib.mapAttrs'
    (name: credential: lib.nameValuePair "llm-credential-${name}" {
      sopsFile = ../../secrets/secrets.yaml;
      key = credential.secretPath;
    }) config.envy.llm.credentials;
}
