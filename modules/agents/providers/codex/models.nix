# Reuse profile inheritance, explicit-null handling, and deduplication.
{ lib }:
{ agent, profiles ? {}, endpoints ? {}, credentials ? {}, agentName ? "codex" }:
let
  selected = import ../pi/models.nix { inherit lib; } {
    inherit agent profiles endpoints credentials agentName;
    defaultApi = "openai-responses";
  };
  first = selected.default.selection;
  path = "envy.agents.${agentName}";
  validate = entry:
    if entry.api != "openai-responses" || (entry.endpoint != null && entry.endpoint.protocol != "openai-compatible") then
      throw "${path}: Codex requires openai-responses on an OpenAI-compatible endpoint; Chat Completions and Anthropic are unsupported."
    else if entry.selection.endpoint != first.endpoint || entry.selection.credential != first.credential then
      throw "${path}.models: Codex models must use the same endpoint and credential."
    else if entry.selection.maxTokens != first.maxTokens then
      throw "${path}.models: Codex models must share one maxTokens limit."
    else true;
in builtins.deepSeq (map validate selected.bindings) selected
