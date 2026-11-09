{ pkgs, piSource }:

pkgs.runCommand "pi-envy-auto-assigned" { } ''
  mkdir -p "$out/src"
  cp ${../../../../files/agents/pi/auto-assigned}/index.ts "$out/index.ts"
  cp ${../../../../files/agents/pi/auto-assigned}/src/pi-version.ts "$out/src/pi-version.ts"
  cp ${piSource}/packages/coding-agent/examples/extensions/questionnaire.ts "$out/questionnaire.ts"
''
