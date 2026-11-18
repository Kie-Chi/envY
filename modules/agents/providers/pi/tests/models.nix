# Evaluate the real provider's managed models.json without activating a host.
{ lib, pkgs, piPackage }:

let
  ordinary = [ "off" "minimal" "low" "medium" "high" ];
  extended = ordinary ++ [ "xhigh" "max" "ultra" ];
  sol = [ "low" "medium" "high" ];
  solExtended = sol ++ [ "xhigh" "max" ];
  fixtureWith = overrides: name: model: api: moreThinking: expectedLevels: defaultEffort:
    let
      agent = {
        adapter = "pi";
        endpoint = "test";
        credential = null;
        contextWindow = 8192;
        maxTokens = 1024;
        inherit model api moreThinking;
      } // overrides;
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
  fixture = fixtureWith {};
  restricted = { off = null; minimal = null; ultra = null; high = "medium"; };
in
[
  (fixture "sol-responses" "gpt-6.1-sol" "openai-responses" false sol null)
  (fixture "sol-alias-responses" "gpt-6.1-sol-qiye" "openai-responses" true solExtended null)
  (fixture "sol-snapshot-responses" "gpt-6.1-sol-2026-09-29" "openai-responses" true solExtended null)
  (fixture "stepcode-sol-responses" "stepcode-gpt-6.1-sol" "openai-responses" false sol null)
  (fixture "stepcode-sol-eastus2-responses-default" "stepcode-gpt-6.1-sol-eastus2" "openai-responses" false sol null)
  (fixture "stepcode-sol-eastus2-completions-default" "stepcode-gpt-6.1-sol-eastus2" "openai-completions" false sol null)
  (fixture "stepcode-sol-eastus2-responses" "stepcode-gpt-6.1-sol-eastus2" "openai-responses" true solExtended null)
  (fixture "stepcode-sol-completions" "stepcode-gpt-6.1-sol" "openai-completions" false sol null)
  (fixture "stepcode-sol-eastus2-completions" "stepcode-gpt-6.1-sol-eastus2" "openai-completions" true solExtended null)
  (fixture "unknown-sol-alias" "stepcode-gpt-6.1-sol-other" "openai-responses" false ordinary "none")
  (fixture "sol-substring" "other-sol-model" "openai-responses" false ordinary "none")
  (fixtureWith { thinkingLevelMap = restricted; } "explicit-responses" "custom-model" "openai-responses" true sol null)
  (fixtureWith { thinkingLevelMap = { off = "low"; }; } "explicit-default-effort" "custom-model" "openai-responses" false ordinary "low")
  (fixtureWith { thinkingLevelMap = restricted; } "explicit-completions" "custom-model" "openai-completions" true sol null)
  (fixtureWith { thinkingLevelMap = {}; } "explicit-overrides-sol" "gpt-6.1-sol" "openai-responses" true ordinary "none")
  (fixtureWith { thinkingLevelMap = null; } "explicit-null-sol" "stepcode-gpt-6.1-sol" "openai-responses" true solExtended null)
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
