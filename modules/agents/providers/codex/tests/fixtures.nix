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
        agentProfiles = profiles;
        llm = { inherit endpoints credentials; };
      };
      sops = { secrets = {}; placeholder = {}; templates = {}; };
      home.activation = {};
    };
  };

  fixture = name: agent: {
    inherit name agent;
    values = flatValues;
    expected = (providerFor agent).config.agents.settingsFiles.codex.render agent;
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
  (fixture "completions" {
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
    profile = "deepseek";
    endpoint = "xikai-openai";
    credential = "xikai";
    model = "gpt-6.1-sol";
  })
]
