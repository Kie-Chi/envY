{ lib, pkgs, darwinConfigurations, homeConfigurations }:

let
  fixturesFor = name: platform: home: casks:
    let names = namesFor home; in {
    inherit name platform;
    serviceSettings = home.envy.t3code.service;
    serviceCommand = toString home.home.file.".local/state/envy/t3code/service".source;
    serviceUnitCommand = if platform == "darwin"
      then builtins.head home.launchd.agents.t3code.config.ProgramArguments
      else builtins.head (lib.toList home.systemd.user.services.t3code.Service.ExecStart);
    serviceUnitName = if platform == "darwin"
      then home.launchd.agents.t3code.config.Label else "t3code.service";
    serviceEnvironment = if platform == "darwin"
      then home.launchd.agents.t3code.config.EnvironmentVariables
      else home.systemd.user.services.t3code.Service.Environment;
    servicePackage = toString home.home.file.".local/state/envy/t3code/package".source;
    serviceHome = "${home.home.homeDirectory}/.t3";
    profileBin = "${home.home.profileDirectory}/bin";
    codexBinary = if platform == "darwin"
      then "${darwinConfigurations.${name}.config.homebrew.prefix}/bin/codex"
      else "${home.home.profileDirectory}/bin/codex";
    providers = lib.optional (builtins.elem "codex" casks || builtins.elem "codex" names) "codex"
      ++ lib.optional (builtins.elem "pi" names) "pi"
      ++ lib.optional (builtins.elem "claude" names) "claudeAgent";
    files = lib.listToAttrs (map (action: lib.nameValuePair action.id action.merge.source)
      (lib.filter (action: action.owner == "modules/agents/providers/t3code/default.nix" && action.merge != null)
        home.envy.machine.activationActions));
  };
  namesFor = home: map lib.getName home.envy.software.nix.packages.effective;
  darwinFixtures = lib.mapAttrsToList (name: configuration:
    let
      home = configuration.config.home-manager.users.${configuration.config.envy.user.name};
    in (fixturesFor name "darwin" home configuration.config.envy.darwin.software.homebrew.casks.effective)
  ) darwinConfigurations;
  linuxFixtures = lib.mapAttrsToList (name: configuration:
    fixturesFor name "linux" configuration.config []
  ) homeConfigurations;
  excluded = (homeConfigurations.linux_desktop.extendModules {
    modules = [{ envy.software.nix.packages.exclude = [ "t3code-nightly" ]; }];
  }).config;
  baseline = homeConfigurations.linux_desktop.config;
  changedNetwork = (homeConfigurations.linux_desktop.extendModules {
    modules = [{ envy.t3code.service.port = lib.mkForce 3774; }];
  }).config;
  changedPackage = (homeConfigurations.linux_desktop.extendModules {
    specialArgs.t3codePackage = pkgs.runCommand "t3code-nightly-activation-fixture" {
      pname = "t3code-nightly";
      version = "activation-fixture";
    } "mkdir -p $out/bin; touch $out/bin/t3";
  }).config;
  runnerOf = home: toString home.home.file.".local/state/envy/t3code/service".source;
  fixtures = if pkgs.stdenv.hostPlatform.isDarwin then darwinFixtures else linuxFixtures;
in
assert lib.all (configuration:
  !(builtins.elem "t3-code" configuration.config.envy.darwin.software.homebrew.casks.effective)
) (builtins.attrValues darwinConfigurations);
assert !(builtins.elem "t3code-nightly" (namesFor excluded));
assert !(excluded.home.activation ? mergeT3Code-t3code-server);
assert !(excluded.home.file ? ".local/bin/t3");
assert !(excluded.home.file ? ".local/state/envy/t3code/package");
assert !(excluded.home.file ? ".local/state/envy/t3code/service");
assert !(excluded.systemd.user.services ? t3code);
assert runnerOf baseline != runnerOf changedNetwork;
assert runnerOf baseline != runnerOf changedPackage;
assert builtins.head (lib.toList changedNetwork.systemd.user.services.t3code.Service.ExecStart) == runnerOf changedNetwork;
assert builtins.head (lib.toList changedPackage.systemd.user.services.t3code.Service.ExecStart) == runnerOf changedPackage;
pkgs.runCommand "envy-t3code-settings" { nativeBuildInputs = [ pkgs.python3 pkgs.nodejs_22 ]; } ''
  export PYTHONDONTWRITEBYTECODE=1
  T3CODE_SERVICE_HELPER=${../modules/agents/providers/t3code/service.mjs} \
    node --test ${../modules/agents/providers/t3code/service-tests/service.test.mjs}
  T3CODE_VERIFY_BUILD=${../modules/agents/providers/t3code/verify_build.py} \
    python -m unittest discover -s ${../modules/agents/providers/t3code/tests} -p 'test_*.py'
  python ${./t3code-settings.py} \
    ${pkgs.writeText "t3code-settings-fixtures.json" (builtins.toJSON fixtures)}
  touch "$out"
''
