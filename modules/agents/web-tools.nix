{ config, lib, machinePlatform, mcpNixosPackage, pkgs, ... }:

let
  readerPython = pkgs.python3.withPackages (ps: [ ps.readability-lxml ps.html2text ]);
  reader = pkgs.writeShellApplication {
    name = "envy-read";
    runtimeInputs = [ pkgs.poppler-utils ];
    checkPhase = ''
      ENVY_READER_PATH=${./skills/read/scripts/read.py} ${readerPython}/bin/python ${./tests/test_reader.py}
    '';
    text = ''
      exec ${readerPython}/bin/python ${./skills/read/scripts/read.py} "$@"
    '';
  };
  nixMcp = mcpNixosPackage.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      substituteInPlace mcp_nixos/sources/flake_inputs.py \
        --replace-fail '["flake", "archive", "--json"]' \
        '["flake", "archive", "--json", "--no-update-lock-file", "--no-write-lock-file"]'
    '';
  });
  browserExecutable = if machinePlatform == "darwin" then
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else lib.getExe pkgs.chromium;
  browser = pkgs.writeShellApplication {
    name = "envy-browser-mcp";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      mode="''${1:-isolated}"
      if [ "$#" -gt 1 ] || { [ "$mode" != isolated ] && [ "$mode" != login ]; }; then
        echo "Usage: envy-browser-mcp [isolated|login]" >&2
        exit 2
      fi
      if [ ! -x ${lib.escapeShellArg browserExecutable} ]; then
        echo "The configured browser is unavailable; no automatic download is performed." >&2
        exit 1
      fi
      umask 077
      output=$(mktemp -d "''${TMPDIR:-/tmp}/envy-browser.XXXXXX")
      args=(--executable-path ${lib.escapeShellArg browserExecutable}
        --sandbox --codegen none --output-dir "$output")
      if [ "$mode" = login ]; then
        profile="$HOME/.local/share/envy/browser/profile"
        mkdir -p "$profile"
        chmod 700 "$profile"
        args+=(--user-data-dir "$profile")
      else
        args+=(--isolated --headless)
      fi
      # Caller overrides must not attach the server to a daily browser/profile.
      for variable in "''${!PLAYWRIGHT_MCP_@}"; do
        unset "$variable"
      done
      unset OPENAI_API_KEY ANTHROPIC_API_KEY
      exec ${lib.getExe pkgs.playwright-mcp} "''${args[@]}"
    '';
  };
  selected = name: !(builtins.elem name config.envy.software.nix.packages.exclude);
  browserTools = lib.genAttrs [
    "browser_navigate" "browser_navigate_back" "browser_snapshot"
    "browser_click" "browser_type" "browser_fill_form" "browser_press_key"
    "browser_take_screenshot" "browser_wait_for" "browser_tabs" "browser_close"
    "browser_select_option" "browser_handle_dialog"
  ] (_: "codemode");
  browserServer = mode: {
    command = lib.getExe browser;
    args = [ mode ];
    description = if mode == "login" then
      "Dedicated visible browser for user-assisted login; never uses the daily browser profile."
      else "Isolated browser for dynamic pages, accessibility snapshots, interaction and screenshots.";
    exposure = "hidden";
    toolExposure = browserTools;
    timeout = 120;
  };
in
{
  envy.software.nix.packages = {
    include = [ reader nixMcp browser ];
    sourceDependencies = {
      mcp-nixos = [ "flake:mcp-nixos#default" ];
      envy-browser-mcp = [ "nix:playwright-mcp" ];
    };
    references = {
      envy-read = "local:modules/agents/web-tools.nix#reader";
      mcp-nixos = "local:modules/agents/web-tools.nix#nixMcp";
      envy-browser-mcp = "local:modules/agents/web-tools.nix#browser";
    };
  };
  envy.agents.pi.runtimeFiles = [ {
    name = "mcp";
    target = ".pi/agent/mcp.json";
    format = "json";
    data.mcpServers = {
      mcp-nixos = {
        command = "${pkgs.coreutils}/bin/env";
        args = [ "-u" "PYTHONPATH" (lib.getExe nixMcp) ];
        description = "Query Nix packages, Home Manager/nix-darwin options and locked flake inputs.";
        enabled = selected "mcp-nixos";
        exposure = "hidden";
        toolExposure = { nix = "codemode"; nix_versions = "codemode"; };
        timeout = 120;
      };
      playwright = browserServer "isolated" // {
        enabled = selected "envy-browser-mcp";
      };
      playwright-login = browserServer "login" // { enabled = false; };
    };
  } ];
}
