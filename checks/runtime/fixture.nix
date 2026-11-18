{ exclusions ? { } }:
{ ... }:

{
  # Both fixtures use the production Home Manager composition from home.nix.
  # The default fixture remains untrimmed and is evaluated, never activated.
  envy = {
    user = {
      name = "envy-runtime";
      home = "/home/envy-runtime";
    };
    repository.path = "/home/envy-runtime/.envy";
    git = {
      name = "envY Runtime Fixture";
      email = "envy-runtime@runtime.invalid";
    };
    vscode.mode = "remote";
    linux = {
      option = "server";
      desktop = "none";
      software = {
        native.repositories.exclude = exclusions."native.system.repository" or [ ];
        native.packages.exclude = exclusions."native.system.package" or [ ];
        url.artifacts.exclude = exclusions."url.system.artifact" or [ ];
      };
    };
    mirrors.mode = "upstream";
    storage = {
      mode = "manual";
      archive = { repository = ""; mountSource = ""; };
    };
    llm = {
      credentials = {
        stepfun = { kind = "api-key"; secretPath = "llm/steps/apikey"; };
        deepseek = { kind = "api-key"; secretPath = "llm/deepseek/apikey"; };
      };
      endpoints = {
        stepfun-openai = { baseUrl = "https://openai.runtime.invalid/v1"; protocol = "openai-compatible"; };
        stepfun-anthropic = { baseUrl = "https://anthropic.runtime.invalid"; protocol = "anthropic-compatible"; };
        deepseek-openai = { baseUrl = "https://deepseek.runtime.invalid/v1"; protocol = "openai-compatible"; };
      };
    };
    software = {
      nix.packages.exclude = exclusions."nix.user.package" or [ ];
      npm.tools.exclude = exclusions."npm.user.tool" or [ ];
      pypi.tools.exclude = exclusions."pypi.user.tool" or [ ];
    };
  };
}
