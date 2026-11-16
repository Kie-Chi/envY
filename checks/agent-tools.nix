{ lib }:

let
  # Only inspect declarations. No package, activation, or user config is run.
  pkgs = {
    stdenv.hostPlatform = { isDarwin = true; isLinux = false; system = "aarch64-darwin"; };
    python3 = "/fixture/python";
    nodejs_26 = "/fixture/node";
  };
  evaluate = policy: (lib.evalModules {
    specialArgs = {
      inherit pkgs;
      machineId = "agent-tools-fixture";
      machinePlatform = "darwin";
      machineSystem = "aarch64-darwin";
      sys.task.activation = spec: spec;
    };
    modules = [
      ../modules/envy/home.nix
      ../modules/agents/tools/default.nix
      ../modules/software/npm.nix
      {
        # These separate tools own packages and MCP servers, not CodeGraph.
        disabledModules = [
          ../modules/agents/tools/obelisk.nix
          ../modules/agents/tools/web.nix
        ];
        config.envy.user.home = "/tmp/envy-agent-tools-fixture";
        options = {
          assertions = lib.mkOption {
            type = lib.types.listOf lib.types.anything;
            default = [ ];
          };
          home = {
            packages = lib.mkOption { type = lib.types.listOf lib.types.package; };
            activation = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = { };
            };
            sessionVariables = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = { };
            };
            sessionPath = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
            };
          };
        };
      }
      policy
    ];
  }).config;
  selected = evaluate { };
  excluded = evaluate { envy.software.npm.tools.exclude = [ "codegraph" ]; };
  empty = evaluate { envy.software.npm.tools.include = lib.mkForce [ ]; };
  other = { id = "other-tool"; name = "other-package"; ref = "npm:other-package"; };
  otherOnly = evaluate {
    envy.software.npm.tools = { include = [ other ]; exclude = [ "codegraph" ]; };
  };
  # A package name/reference alone must not impersonate the selected stable ID.
  samePackageOtherId = evaluate {
    envy.software.npm.tools.include = lib.mkForce [ {
      id = "other-codegraph";
      name = "@colbymchenry/codegraph";
      ref = "npm:@colbymchenry/codegraph";
    } ];
  };
  expected = {
    name = "codegraph-install";
    after = [ "installNpmTools" ];
    script = ''
      CODEX_CONFIG="$HOME/.codex/config.toml"
      CODEGRAPH_NORMALIZER=${../resources/helpers/codegraph-codex.py}

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
  noCodegraphHook = config:
    let scripts = lib.concatMapStringsSep "\n" (entry: entry.script)
      (builtins.attrValues config.home.activation);
    in !(config.home.activation ? installCodegraphMCP)
      && !(lib.hasInfix "codegraph-codex.py" scripts)
      && !(lib.hasInfix "/bin/node " scripts)
      && !(lib.hasInfix ".codex/config.toml" scripts);
in
assert lib.all (config: lib.all (item: item.assertion) config.assertions)
  [ selected excluded empty otherOnly samePackageOtherId ];
assert map (item: item.id) selected.envy.software.npm.tools.effective == [ "codegraph" ];
assert selected.home.activation.installCodegraphMCP == expected;
assert selected.home.activation ? installNpmTools;
assert excluded.envy.software.npm.tools.effective == [ ];
assert empty.envy.software.npm.tools.effective == [ ];
assert lib.all noCodegraphHook [ excluded empty otherOnly samePackageOtherId ];
assert excluded.home.activation == { };
assert empty.home.activation == { };
assert map (item: item.id) otherOnly.envy.software.npm.tools.effective == [ "other-tool" ];
assert map (item: item.id) samePackageOtherId.envy.software.npm.tools.effective == [ "other-codegraph" ];
assert otherOnly.home.activation ? installNpmTools;
assert samePackageOtherId.home.activation ? installNpmTools;
assert selected.envy.software.npm.tools.include == [ {
  id = "codegraph";
  name = "@colbymchenry/codegraph";
  version = "0.9.7";
  ref = "npm:@colbymchenry/codegraph";
  sourceId = null;
  parameters = { };
} ];
assert lib.all (config: config.envy.software.pypi.tools.effective == [ {
  id = "headroom";
  name = "headroom-ai";
  version = null;
  ref = "pypi:headroom-ai";
  sourceId = null;
  parameters."with" = [ "fastapi" ];
} ]) [ selected excluded empty otherOnly samePackageOtherId ];
true
