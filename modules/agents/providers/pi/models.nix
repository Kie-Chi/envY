# Normalize legacy and list selections before rendering Pi configuration.
{ lib }:
{ agent, profiles ? {}, endpoints ? {}, credentials ? {} }:
let
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
  select = value: fields // defaults // builtins.intersectAttrs fields value;
  fromProfile = name:
    if builtins.hasAttr name profiles then select (lib.filterAttrs
      (field: value: !(builtins.hasAttr field defaults) || value != null) profiles.${name})
    else throw "Pi model selection references an unknown profile.";
  resolve = overrides: value:
    if (value.profile or null) == null then select value
    else fromProfile value.profile // lib.optionalAttrs overrides (builtins.intersectAttrs fields value);
  multi = (agent.models or []) != [];
  primary = resolve false agent;
  candidates = if multi then
    lib.optional (primary.model != null) primary ++ map (resolve true) agent.models
    else [ primary ];
  binding = selection: let
    endpoint = if selection.endpoint == null then null else endpoints.${selection.endpoint} or null;
    api = if selection.api != null then selection.api
      else if endpoint != null && endpoint.protocol == "anthropic-compatible" then "anthropic-messages"
      else "openai-completions";
    identity = builtins.substring 0 16 (builtins.hashString "sha256"
      (builtins.toJSON [ selection.endpoint api selection.credential ]));
    provider = if multi then "envy-pi-${identity}" else "envy-pi";
    keyVariable = if multi then "ENVY_PI_KEY_${lib.toUpper identity}"
      else if api == "anthropic-messages" then "ANTHROPIC_API_KEY" else "OPENAI_API_KEY";
    valid = !multi || (
      selection.model != null && selection.model != ""
      && endpoint != null
      && (selection.credential == null || builtins.hasAttr selection.credential credentials)
    );
  in if !valid then throw "Pi models require a model, a known endpoint, and a known credential reference or null."
    else {
      inherit endpoint api provider keyVariable;
      selection = selection // { inherit api; };
      apiKey = if multi && selection.credential == null then "envy-no-key" else "$" + keyVariable;
    };
  bindings = builtins.foldl' (acc: item: let
    entry = binding item;
    matching = lib.filter (other: other.provider == entry.provider && other.selection.model == entry.selection.model) acc;
  in if matching == [] then acc ++ [ entry ]
    else if (builtins.head matching).selection == entry.selection
    then acc
    else throw "Pi models contain conflicting metadata for the same connection and model."
  ) [] candidates;
in {
  inherit bindings multi;
  default = if bindings == [] then null else builtins.head bindings;
}
