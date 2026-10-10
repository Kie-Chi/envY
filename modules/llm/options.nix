{ lib, ... }:

let
  inherit (lib) mkOption types;
in
{
  options.envy.llm = {
    credentials = mkOption {
      type = types.attrsOf (types.submodule ({ ... }: {
        options = {
          kind = mkOption {
            type = types.enum [ "api-key" "token" "basic" ];
            default = "api-key";
            description = "Kind of encrypted credential consumed by an agent adapter.";
          };
          secretPath = mkOption {
            type = types.nonEmptyStr;
            description = "Slash-separated path in the encrypted sops YAML file.";
          };
        };
      }));
      default = { };
      description = "Logical references to encrypted LLM credentials; values never enter Nix evaluation.";
    };

    endpoints = mkOption {
      type = types.attrsOf (types.submodule ({ ... }: {
        options = {
          baseUrl = mkOption {
            type = types.nonEmptyStr;
            description = "Non-sensitive API endpoint base URL.";
          };
          protocol = mkOption {
            type = types.enum [ "openai-compatible" "anthropic-compatible" "native" ];
            default = "openai-compatible";
            description = "Wire protocol expected by the endpoint.";
          };
        };
      }));
      default = { };
      description = "Named LLM endpoints shared by agent instances.";
    };
  };
}
