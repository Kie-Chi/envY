# Evaluate the real provider's managed models.json without activating a host.
{ lib, pkgs, piPackage }:

let
  ordinary = [ "off" "minimal" "low" "medium" "high" ];
  extended = ordinary ++ [ "xhigh" "max" "ultra" ];
  sol = [ "low" "medium" "high" ];
  solExtended = sol ++ [ "xhigh" "max" ];
  fixture = name: model: api: moreThinking: expectedLevels: defaultEffort:
    let
      agent = {
        adapter = "pi";
        endpoint = "test";
        credential = null;
        contextWindow = 8192;
        maxTokens = 1024;
        inherit model api moreThinking;
      };
      provider = import ../default.nix {
        inherit lib pkgs piPackage;
        config.envy = {
          agents.pi = agent;
          llm.endpoints.test = {
            baseUrl = "http://127.0.0.1:1/v1";
            protocol = if api == "anthropic-messages" then "anthropic-compatible" else "openai-compatible";
          };
        };
      };
      managed = builtins.head provider.config.envy.agents.pi.runtimeFiles;
    in {
      inherit name agent expectedLevels defaultEffort;
      data = managed.data;
    };
in
[
  (fixture "sol-responses" "gpt-6.1-sol" "openai-responses" false sol null)
  (fixture "sol-alias-responses" "gpt-6.1-sol-qiye" "openai-responses" true solExtended null)
  (fixture "sol-snapshot-responses" "gpt-6.1-sol-2026-09-29" "openai-responses" true solExtended null)
  (fixture "sol-completions" "gpt-6.1-sol" "openai-completions" false sol null)
  (fixture "sol-alias-completions" "gpt-6.1-sol-qiye" "openai-completions" true solExtended null)
  (fixture "other-responses" "gpt-6-astra" "openai-responses" false ordinary "none")
  (fixture "other-extended-responses" "gpt-6-astra" "openai-responses" true extended "none")
  (fixture "other-completions" "gpt-6-astra" "openai-completions" true extended null)
  (fixture "prefix-boundary" "gpt-6.1-solaris" "openai-responses" true extended "none")
  (fixture "anthropic" "claude-opus-5-5" "anthropic-messages" false (lib.tail ordinary) null)
  (fixture "anthropic-extended" "claude-opus-5-5" "anthropic-messages" true (lib.tail extended) null)
  (fixture "anthropic-api-boundary" "gpt-6.1-sol" "anthropic-messages" true (lib.tail extended) null)
] ++ import ./model-lists.nix { inherit lib pkgs piPackage; }
