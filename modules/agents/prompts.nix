{ lib, pkgs }:

let
  common = builtins.readFile ./prompts/common.md;
  forAdapter = adapter: lib.concatStringsSep "\n\n" ([ common ]
    ++ lib.optional (adapter == "claude-code") (builtins.readFile ./prompts/claude.md)
    ++ lib.optional (adapter == "pi") (builtins.readFile ./prompts/pi.md)
    ++ lib.optional (adapter == "codex") (builtins.readFile ./prompts/codex.md));
  fileFor = adapter: pkgs.writeText "envy-${adapter}-system-prompt" (forAdapter adapter);
  argsFor = adapter:
    if adapter == "pi" then [ "--append-system-prompt" (toString (fileFor adapter)) ]
    else if adapter == "codex" then [ "-c" "developer_instructions=${builtins.toJSON (forAdapter adapter)}" ]
    else [ "--system-prompt" (forAdapter adapter) ];
in
{
  inherit forAdapter fileFor;
  shellArgs = adapter: lib.concatMapStringsSep "\n"
    (arg: "cmd+=(${lib.escapeShellArg arg})") (argsFor adapter);
}
