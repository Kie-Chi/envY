# Behavior of the shared agent wrapper, exercised with a fake agent command.
{ lib, pkgs }:

let
  wrapper = import ../wrapper.nix { inherit lib pkgs; };
  # The wrapper expands env file paths at run time, so the test can place
  # them in its own temporary directory.
  root = "$WRAPPER_TEST_ROOT";
  fakeAgent = pkgs.writeShellScript "fake-agent" ''
    printf 'arg:%s\n' "$@"
    printf 'sourced:%s\n' "''${SOURCED:-none}"
    printf 'value:%s\n' "''${VALUE:-unset}"
  '';
  plain = wrapper.mkWrapper {
    name = "plain-agent";
    executable = "${fakeAgent}";
    envFiles = [ "${root}/template.env" "${root}/runtime.env" ];
    args = [ "--fixed" "two words" ];
  };
  hooked = wrapper.mkWrapper {
    name = "hooked-agent";
    executable = "${fakeAgent}";
    envFiles = [ "${root}/template.env" ];
    beforeEnv = ''
      case "''${1-}" in
        manage) exec ${fakeAgent} managed "$@" ;;
      esac
    '';
    beforeArgs = ''cmd+=(--model "''${ENVY_AGENT_MODEL:-default}")'';
    args = [ "--fixed" ];
    afterArgs = "cmd+=(--last)";
  };
  aliases = wrapper.mkAliases "plain-agent" [ "pa" ];
in
pkgs.writeShellScript "agent-wrapper-tests" ''
  set -euo pipefail
  WRAPPER_TEST_ROOT="$(mktemp -d)"
  export WRAPPER_TEST_ROOT
  PATH="${lib.makeBinPath ([ plain hooked ] ++ aliases)}:$PATH"
  failures=0

  check() {
    local label="$1" expected="$2" actual
    shift 2
    actual="$("$@")"
    if [ "$actual" != "$expected" ]; then
      printf 'FAIL %s\n--- expected\n%s\n--- actual\n%s\n' "$label" "$expected" "$actual" >&2
      failures=$((failures + 1))
    fi
  }

  check "missing env files are skipped and caller arguments follow fixed ones" \
    "$(printf '%s\n' 'arg:--fixed' 'arg:two words' 'arg:a' 'arg:b c' 'sourced:none' 'value:unset')" \
    plain-agent a "b c"

  printf '%s\n' 'SOURCED="''${SOURCED:+$SOURCED,}template"' 'export SOURCED' 'export VALUE=template' \
    > "$WRAPPER_TEST_ROOT/template.env"
  check "a single env file is sourced once" \
    "$(printf '%s\n' 'arg:--fixed' 'arg:two words' 'sourced:template' 'value:template')" \
    plain-agent

  printf '%s\n' 'SOURCED="''${SOURCED:+$SOURCED,}runtime"' 'export SOURCED' 'export VALUE=runtime' \
    > "$WRAPPER_TEST_ROOT/runtime.env"
  check "env files are sourced once each and later files override earlier ones" \
    "$(printf '%s\n' 'arg:--fixed' 'arg:two words' 'sourced:template,runtime' 'value:runtime')" \
    plain-agent

  check "hooks surround the fixed arguments" \
    "$(printf '%s\n' 'arg:--model' 'arg:default' 'arg:--fixed' 'arg:--last' 'arg:x' 'sourced:template' 'value:template')" \
    hooked-agent x
  check "environment selects the model at run time" \
    "$(printf '%s\n' 'arg:--model' 'arg:m' 'arg:--fixed' 'arg:--last' 'sourced:template' 'value:template')" \
    env ENVY_AGENT_MODEL=m hooked-agent
  check "the pre-env hook can bypass env files and arguments" \
    "$(printf '%s\n' 'arg:managed' 'arg:manage' 'arg:y' 'sourced:none' 'value:unset')" \
    hooked-agent manage y

  check "aliases forward to the wrapper" \
    "$(printf '%s\n' 'arg:--fixed' 'arg:two words' 'arg:z' 'sourced:template,runtime' 'value:runtime')" \
    pa z

  rm -rf "$WRAPPER_TEST_ROOT"
  if [ "$failures" -ne 0 ]; then
    echo "$failures agent wrapper check(s) failed" >&2
    exit 1
  fi
  echo "agent wrapper checks passed"
''
