{ ... }:

{
  # Darwin distribution for agent applications. Agent configuration and
  # skills remain cross-platform in modules/agents/defaults.nix.
  envy.darwin.software.homebrew.casks.include = [
    "codex"
    "chatgpt"
  ];
}
