# Evaluate the real Codex provider without activating a host.
{ lib, pkgs }:

let
  endpoints = {
    xikai-openai = { baseUrl = "https://api.xikai.site/v1"; protocol = "openai-compatible"; };
    deepseek-openai = { baseUrl = "https://api.deepseek.com"; protocol = "openai-compatible"; };
    stepfun-openai = { baseUrl = "https://stepfun.com/v1"; protocol = "openai-compatible"; };
    anthropic-example = { baseUrl = "https://anthropic.example.invalid"; protocol = "anthropic-compatible"; };
  };

  credentials = {
    xikai.secretPath = "llm/xikai/apikey";
    deepseek.secretPath = "llm/deepseek/apikey";
  };

  profiles = {
    responses = {
      endpoint = "xikai-openai";
      credential = "xikai";
      api = "openai-responses";
      model = "gpt-6.1-sol";
    };
    deepseek = {
      endpoint = "deepseek-openai";
      credential = "deepseek";
      api = "openai-completions";
      model = "deepseek-v4-pro";
    };
  };

  # Flat managed-block values, so the Python side can rebuild the same inputs.
  valueList = prefix: attrs: lib.concatLists (lib.mapAttrsToList (name: fields:
    map (field: { path = "envy.${prefix}.${name}.${field}"; value = fields.${field}; })
      (builtins.attrNames fields)) attrs);

  flatValues =
    valueList "llm.endpoints" endpoints
    ++ valueList "llm.credentials" credentials
    ++ valueList "agentProfiles" profiles;

  providerFor = agent: import ../../codex.nix {
    inherit lib pkgs;
    config = {
      envy = {
        agents.codex = agent;
        user.home = "/test-home";
        agentProfiles = profiles;
        llm = { inherit endpoints credentials; };
      };
      sops = { secrets = {}; placeholder = {}; templates = {}; };
      home.activation = {};
    };
  };

  defaultsModule = import ../../../defaults.nix;
  defaultsArgs = lib.mapAttrs (_: _: null) (builtins.functionArgs defaultsModule);
  withDefaults = agent: (lib.evalModules {
    modules = [
      ../../../options.nix
      ../../../../llm/options.nix
      ({ config, ... }: { envy = (defaultsModule (defaultsArgs // { inherit lib config; })).envy; })
      { envy = { agentProfiles = profiles; llm = { inherit endpoints credentials; }; agents.codex = agent; }; }
    ];
  }).config.envy.agents.codex;

  fixture = name: agent: {
    inherit name agent;
    values = flatValues;
    expected = (providerFor agent).config.agents.settingsFiles.codex.render agent;
    selections = map (entry: entry.selection) (import ../models.nix { inherit lib; } {
      inherit agent endpoints credentials profiles;
    }).bindings;
  };
  invalid = name: agent: assert !(builtins.tryEval (builtins.deepSeq (fixture name agent) true)).success; {
    inherit name agent;
    invalid = true;
    values = flatValues;
  };
in
[
  (fixture "responses" {
    adapter = "codex";
    profile = null;
    endpoint = "xikai-openai";
    credential = "xikai";
    model = "gpt-6.1-sol";
    api = "openai-responses";
  })
  (fixture "default-wire-api" {
    adapter = "codex";
    profile = null;
    endpoint = "xikai-openai";
    credential = "xikai";
    model = "gpt-6.1-sol";
    api = null;
  })
  (invalid "completions" {
    adapter = "codex";
    profile = null;
    endpoint = "deepseek-openai";
    credential = "deepseek";
    model = "deepseek-v4-pro";
    api = "openai-completions";
  })
  (fixture "no-credential" {
    adapter = "codex";
    profile = null;
    credential = null;
    endpoint = "stepfun-openai";
    model = "step-3.7-flash";
    api = "openai-responses";
  })
  (fixture "limits" {
    adapter = "codex";
    profile = null;
    endpoint = "xikai-openai";
    credential = "xikai";
    model = "gpt-6.1-sol";
    api = "openai-responses";
    contextWindow = 272000;
    maxTokens = 128000;
  })
  (fixture "profile-wins" {
    adapter = "codex";
    profile = "responses";
    endpoint = "xikai-openai";
    credential = "xikai";
    model = "gpt-6.1-sol";
  })
  (fixture "schema-list-only" (withDefaults {
    models = [ { profile = "responses"; } { profile = "responses"; model = "other"; } ];
  }))
  (fixture "list-only" {
    adapter = "codex";
    profile = null;
    models = [ { profile = "responses"; } { profile = "responses"; model = "other"; contextWindow = 10000; } ];
  })
  (fixture "profile-with-list" {
    adapter = "codex";
    profile = "responses";
    models = [ { profile = "responses"; } { profile = "responses"; model = "other"; } ];
  })
  (fixture "explicit-null" {
    adapter = "codex";
    profile = null;
    models = [ { profile = "responses"; credential = null; api = null; contextWindow = null; maxTokens = null; } ];
  })
  (invalid "mixed-endpoint" {
    adapter = "codex";
    profile = "responses";
    models = [ { profile = "responses"; endpoint = "stepfun-openai"; } ];
  })
  (invalid "mixed-credential" {
    adapter = "codex";
    profile = "responses";
    models = [ { profile = "responses"; credential = "deepseek"; } ];
  })
  (invalid "completions-profile" { adapter = "codex"; profile = "deepseek"; })
  (invalid "anthropic-endpoint" {
    adapter = "codex"; profile = null; endpoint = "anthropic-example"; model = "claude-test"; api = "openai-responses";
  })
  (invalid "mixed-output-limits" {
    adapter = "codex"; profile = "responses";
    models = [ { profile = "responses"; model = "other"; maxTokens = 100; } ];
  })
  (invalid "conflicting-metadata" {
    adapter = "codex";
    profile = "responses";
    models = [ { profile = "responses"; contextWindow = 1; } ];
  })
]
