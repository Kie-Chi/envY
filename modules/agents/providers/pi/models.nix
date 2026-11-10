# Normalize legacy and list selections before rendering Pi configuration.
{ lib }:
{ agent, profiles ? {}, endpoints ? {}, credentials ? {}, agentName ? "pi" }:
let
  root = "envy.agents.${agentName}";
  fields = {
    endpoint = null;
    credential = null;
    api = null;
    model = null;
    contextWindow = null;
    maxTokens = null;
  };
  defaults = {
    contextWindow = agent.contextWindow or null;
    maxTokens = agent.maxTokens or null;
  };
  fail = code: path: message: hint:
    throw ("[envy-config] " + builtins.toJSON { inherit code path message hint; }
      + "\n" + message + "\nLocation: " + path + "\nNext: " + hint);
  select = value: fields // defaults // builtins.intersectAttrs fields value;
  resolve = overrides: value: path: let
    profile = value.profile or null;
    prefix = "envy.agentProfiles.${profile}";
    inherited = if profile == null then {} else if builtins.hasAttr profile profiles
      then lib.filterAttrs (field: v: !(builtins.hasAttr field defaults) || v != null) profiles.${profile}
      else fail "unknown-reference" "${path}.profile" "profile '${profile}' is not declared"
        "Declare ${prefix} first, or correct the reference at ${path}.profile.";
    explicit = if overrides then builtins.intersectAttrs fields value else {};
  in {
    inherit path;
    selection = if profile == null then select value else select inherited // explicit;
    origins = lib.mapAttrs (field: _: "${path}.${field}") fields
      // lib.mapAttrs (field: _: "${prefix}.${field}") (builtins.intersectAttrs fields inherited)
      // lib.mapAttrs (field: _: "${path}.${field}") explicit;
  };
  multi = (agent.models or []) != [];
  primary = resolve false agent root;
  candidates = if multi then
    lib.optional (primary.selection.model != null) primary
    ++ lib.imap0 (index: value: resolve true value "${root}.models[${toString index}]") agent.models
    else [ primary ];
  binding = item: let
    inherit (item) selection path origins;
    endpoint = if selection.endpoint == null then null else endpoints.${selection.endpoint} or null;
    withSource = field: message: message + lib.optionalString (origins.${field} != "${path}.${field}")
      " (inherited from ${origins.${field}})";
    problem = field: code: message: hint: fail code "${path}.${field}" (withSource field message) hint;
    endpointPath = "envy.llm.endpoints.${selection.endpoint}.baseUrl";
    credentialPath = "envy.llm.credentials.${selection.credential}.secretPath";
    valid = if multi && (selection.model == null || selection.model == "") then
        problem "model" "missing-required" "model is not set" "Set ${origins.model} to the intended model ID."
      else if multi && selection.endpoint == null then
        problem "endpoint" "missing-required" "endpoint is not set" "Set ${origins.endpoint} to a declared endpoint name."
      else if selection.endpoint != null && endpoint == null then
        problem "endpoint" "unknown-reference" "endpoint '${selection.endpoint}' is not declared"
          "Declare ${endpointPath} first, or correct the reference at ${origins.endpoint}."
      else if endpoint != null && (endpoint.baseUrl or "") == "" then
        problem "endpoint" "missing-required" "endpoint '${selection.endpoint}' baseUrl is not set"
          "Set ${endpointPath} to the endpoint URL."
      else if selection.credential != null && !(builtins.hasAttr selection.credential credentials) then
        problem "credential" "unknown-reference" "credential '${selection.credential}' is not declared"
          "Declare ${credentialPath} first, or correct the reference at ${origins.credential}."
      else if selection.credential != null && (credentials.${selection.credential}.secretPath or "") == "" then
        problem "credential" "missing-required" "credential '${selection.credential}' secretPath is not set"
          "Set ${credentialPath} to the encrypted YAML path; the key value belongs in sops, not Nix."
      else true;
    api = if selection.api != null then selection.api
      else if endpoint != null && endpoint.protocol == "anthropic-compatible" then "anthropic-messages"
      else "openai-completions";
    identity = builtins.substring 0 16 (builtins.hashString "sha256"
      (builtins.toJSON [ selection.endpoint api selection.credential ]));
    provider = if multi then "envy-pi-${identity}" else "envy-pi";
    keyVariable = if multi then "ENVY_PI_KEY_${lib.toUpper identity}"
      else if api == "anthropic-messages" then "ANTHROPIC_API_KEY" else "OPENAI_API_KEY";
  in builtins.seq valid {
    inherit endpoint api provider keyVariable;
    selection = selection // { inherit api; };
    apiKey = if multi && selection.credential == null then "envy-no-key" else "$" + keyVariable;
  };
  resolved = builtins.foldl' (acc: item: let
    entry = binding item;
    matching = lib.filter (other: other.entry.provider == entry.provider && other.entry.selection.model == entry.selection.model) acc;
    previous = builtins.head matching;
    differing = lib.filter (field: previous.entry.selection.${field} != entry.selection.${field}) (builtins.attrNames fields);
  in if matching == [] then acc ++ [ { inherit entry; path = item.path; } ]
    else if previous.entry.selection == entry.selection then acc
    else fail "invalid-value" item.path
      "models contain conflicting metadata for the same connection and model: ${previous.path} and ${item.path} differ in ${lib.concatStringsSep ", " differing}"
      "Keep one entry or align those fields."
  ) [] candidates;
  bindings = map (item: item.entry) resolved;
in {
  inherit bindings multi;
  default = if bindings == [] then null else builtins.head bindings;
}
