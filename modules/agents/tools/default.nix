{ pkgs, sys, ... }:

{
  imports = [ ./web.nix ];

  # Registry-installed agent helpers are platform-neutral. Their installation
  # lifecycle belongs to modules/software; this feature owns only selection and
  # the CodeGraph-specific MCP registration step.
  envy.software.npm.tools.include = [
    {
      id = "codegraph";
      name = "@colbymchenry/codegraph";
      version = "0.9.7";
      ref = "npm:@colbymchenry/codegraph";
    }
  ];

  envy.software.pypi.tools.include = [
    {
      id = "headroom";
      name = "headroom-ai";
      ref = "pypi:headroom-ai";
      parameters."with" = [ "fastapi" ];
    }
  ];

  home.activation.installCodegraphMCP = sys.task.activation {
    name = "codegraph-install";
    after = [ "installNpmTools" ];
    script = ''
      CODEX_CONFIG="$HOME/.codex/config.toml"
      CODEGRAPH_NORMALIZER=${../../../resources/helpers/codegraph-codex.py}

      # Codex formats nested MCP tables with indentation, but CodeGraph 0.9.7
      # only recognizes a table header at column zero. Ensure a canonical entry
      # exists before install so CodeGraph cannot append a duplicate TOML key.
      ${pkgs.python3}/bin/python "$CODEGRAPH_NORMALIZER" "$CODEX_CONFIG"

      if ${pkgs.nodejs_26}/bin/node \
        "$HOME/.npm-global/lib/node_modules/@colbymchenry/codegraph/npm-shim.js" \
        install -y; then
        CODEGRAPH_STATUS=0
      else
        CODEGRAPH_STATUS=$?
      fi

      # Also clean up files produced by an older activation or the upstream
      # installer, while preserving all sibling MCP tables.
      ${pkgs.python3}/bin/python "$CODEGRAPH_NORMALIZER" "$CODEX_CONFIG"
      test "$CODEGRAPH_STATUS" -eq 0
    '';
  };
}
