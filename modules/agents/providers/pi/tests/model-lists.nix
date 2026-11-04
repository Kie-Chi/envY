# Exercise the option schema and shared defaults, not hand-normalized agents.
{ lib, pkgs, piPackage }:
let
  defaultsModule = import ../../../defaults.nix;
  defaultsArgs = lib.mapAttrs (_: _: null) (builtins.functionArgs defaultsModule);
  base = {
    llm = {
      endpoints.a = { baseUrl = "https://a.example/v1"; protocol = "openai-compatible"; };
      endpoints.b = { baseUrl = "https://b.example"; protocol = "anthropic-compatible"; };
      credentials.a.secretPath = "llm/a/key";
      credentials.b.secretPath = "llm/b/key";
    };
    agentProfiles.fast = {
      endpoint = "a";
      credential = "a";
      api = "openai-responses";
      model = "gpt-6.1-sol";
    };
    agentProfiles.partial = {
      endpoint = "a";
      credential = "a";
      api = "openai-responses";
      model = "partial-model";
      contextWindow = 10000;
    };
    agentProfiles.budget = {
      endpoint = "a";
      credential = "a";
      api = "openai-responses";
      model = "budget-model";
      contextWindow = 10000;
      maxTokens = 1000;
    };
  };
  candidates = [
    { profile = "fast"; }
    { model = "claude-test"; endpoint = "b"; credential = "b"; }
    { model = "other"; endpoint = "a"; credential = "a"; api = "openai-responses"; }
  ];
  inline = { model = "same"; endpoint = "a"; credential = "a"; };
  commonLimits = { contextWindow = 272000; maxTokens = 128000; };
  candidateLimits = lib.genAttrs [ "gpt-6.1-sol" "claude-test" "other" ] (_: commonLimits);
  fixture = name: pi: expectedProviders: expectedModels: expectedDefault: let
    evaluated = lib.evalModules {
      modules = [
        ../../../options.nix
        ../../../../llm/options.nix
        ({ config, ... }: { envy = (defaultsModule (defaultsArgs // { inherit lib config; })).envy; })
        { envy = base // { agents.pi = pi; }; }
      ];
    };
    envy = evaluated.config.envy;
    agent = envy.agents.pi;
    flattened = prefix: attrs: lib.concatLists (lib.mapAttrsToList (name: fields:
      lib.mapAttrsToList (field: value: lib.nameValuePair (prefix + name + "." + field) value) fields
    ) attrs);
    values = lib.listToAttrs (
      flattened "envy.llm.endpoints." envy.llm.endpoints
      ++ flattened "envy.llm.credentials." envy.llm.credentials
      ++ flattened "envy.agentProfiles." envy.agentProfiles
    );
    selected = import ../models.nix { inherit lib; } {
      inherit agent;
      inherit (envy.llm) endpoints credentials;
      profiles = envy.agentProfiles;
    };
    provider = import ../default.nix {
      inherit lib piPackage;
      pkgs = pkgs // { writeShellApplication = args: args; };
      config = {
        envy = envy // { user.home = "/test-home"; };
        sops = {
          placeholder = lib.mapAttrs' (name: _: lib.nameValuePair ("llm-credential-" + name) ("fake-key-" + name)) envy.llm.credentials;
          templates.envy-agent-pi.path = "/test-template";
        };
      };
    };
    managed = builtins.head provider.config.envy.agents.pi.runtimeFiles;
  in {
    inherit name agent values expectedProviders expectedModels expectedDefault;
    modelListFixture = true;
    requestedModels = pi.models or [];
    data = managed.data;
    defaultProvider = selected.default.provider;
    defaultModel = selected.default.selection.model;
    template = provider.config.sops.templates.envy-agent-pi.content;
    wrapper = builtins.unsafeDiscardStringContext (builtins.head provider.config.envy.software.nix.packages.include).text;
    inherit (managed) managedKeyPrefixes;
  };
  invalid = pi: !(builtins.tryEval (builtins.deepSeq (fixture "invalid" pi 0 0 "") true)).success;
in
assert lib.all invalid [
  { models = [ {} ]; }
  { models = [ { profile = "missing"; } ]; }
  { models = [ { profile = "fast"; model = null; } ]; }
  { models = [ { profile = "fast"; endpoint = null; } ]; }
  { models = [ { profile = "fast"; endpoint = "missing"; } ]; }
  { models = [ { profile = "fast"; credential = "missing"; } ]; }
  { models = [ { profile = "fast"; api = "invalid"; } ]; }
  { models = [ { profile = "fast"; maxTokens = 0; } ]; }
  { models = [ { profile = "fast"; maxTokens = true; } ]; }
  { models = [ { profile = "fast"; apiKey = "not-allowed"; } ]; }
  { models = [ { profile = "fast"; } { profile = "fast"; contextWindow = 1000; } ]; }
];
[
  (fixture "list-only" { models = candidates; } 2 3 "gpt-6.1-sol")
  (fixture "list-reordered" { models = lib.reverseList candidates; } 2 3 "other")
  (fixture "profile-and-list" { profile = "fast"; model = "stale"; contextWindow = 999; models = candidates; } 2 3 "gpt-6.1-sol")
  (fixture "legacy-and-list" { endpoint = "a"; credential = "a"; model = "legacy"; models = candidates; } 3 4 "legacy")
  (fixture "implicit-endpoint-default" { model = "legacy"; models = candidates; } 3 4 "legacy")
  (fixture "same-model-distinct-connections" { models = [
    inline
    (inline // { api = "openai-responses"; })
    (inline // { endpoint = "b"; })
    (inline // { credential = "b"; })
    inline
  ]; } 4 4 "same")
  (fixture "unauthenticated-endpoint" { models = [ { model = "local"; endpoint = "a"; } ]; } 1 1 "local")
  (fixture "empty-list-legacy" { profile = "fast"; models = []; } 1 1 "gpt-6.1-sol")
  (fixture "empty-list-defaults" { models = []; } 1 1 "gpt-6.1-sol")
  (fixture "profile-model-override" { models = [ { profile = "fast"; } { profile = "fast"; model = "other"; } ]; } 1 2 "gpt-6.1-sol")
  (fixture "profile-metadata-override" { models = [ { profile = "budget"; model = "other"; maxTokens = 2000; } ]; } 1 1 "other")
  (fixture "profile-explicit-null" { models = [ { profile = "budget"; api = null; credential = null; contextWindow = null; maxTokens = null; } ]; } 1 1 "budget-model")
  (fixture "profile-endpoint-override" { models = [ { profile = "budget"; endpoint = "b"; credential = "b"; api = null; } ]; } 1 1 "budget-model")
  ((fixture "pi-limits-propagated" (commonLimits // { profile = "fast"; models = candidates; }) 2 3 "gpt-6.1-sol") // {
    expectedLimits = candidateLimits;
  })
  ((fixture "list-only-pi-limits" (commonLimits // { models = candidates; }) 2 3 "gpt-6.1-sol") // {
    expectedLimits = candidateLimits;
  })
  ((fixture "legacy-pi-limits" (commonLimits // {
    endpoint = "a"; credential = "a"; model = "legacy"; models = candidates;
  }) 3 4 "legacy") // {
    expectedLimits = candidateLimits // { legacy = commonLimits; };
  })
  ((fixture "single-profile-pi-limits" (commonLimits // { profile = "fast"; }) 1 1 "gpt-6.1-sol") // {
    expectedLimits."gpt-6.1-sol" = commonLimits;
  })
  ((fixture "profile-overrides-pi-limits" (commonLimits // { profile = "budget"; models = candidates; }) 2 4 "budget-model") // {
    expectedLimits = candidateLimits // { budget-model = { contextWindow = 10000; maxTokens = 1000; }; };
  })
  ((fixture "profile-inherits-missing-pi-limit" (commonLimits // { profile = "partial"; models = candidates; }) 2 4 "partial-model") // {
    expectedLimits = candidateLimits // { partial-model = { contextWindow = 10000; maxTokens = 128000; }; };
  })
  ((fixture "entry-overrides-pi-limits" (commonLimits // { models = [
    { profile = "budget"; model = "other"; maxTokens = 2000; }
    { model = "local"; endpoint = "b"; maxTokens = 4000; }
  ]; }) 2 2 "other") // {
    expectedLimits = {
      other = { contextWindow = 10000; maxTokens = 2000; };
      local = { contextWindow = 272000; maxTokens = 4000; };
    };
  })
  ((fixture "entry-clears-pi-limits" (commonLimits // { models = [
    { profile = "budget"; contextWindow = null; maxTokens = null; }
    { model = "local"; endpoint = "b"; contextWindow = null; maxTokens = 4000; }
  ]; }) 2 2 "budget-model") // {
    expectedLimits = {
      budget-model = { contextWindow = null; maxTokens = null; };
      local = { contextWindow = null; maxTokens = 4000; };
    };
  })
  ((fixture "pi-limits-without-connection-inheritance" (commonLimits // {
    endpoint = "a"; credential = "a"; api = "openai-responses";
    models = [ { model = "local"; endpoint = "b"; } ];
  }) 1 1 "local") // {
    expectedLimits.local = commonLimits;
  })
  (fixture "profile-resolved-deduplication" { models = [
    { profile = "fast"; model = "gpt-6.1-sol"; }
    { model = "gpt-6.1-sol"; endpoint = "a"; credential = "a"; api = "openai-responses"; }
  ]; } 1 1 "gpt-6.1-sol")
]
