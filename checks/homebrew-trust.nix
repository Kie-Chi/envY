{ lib, darwinConfigurations }:

let
  tap = "vendor/tools";
  host = darwinConfigurations.macbook_air.extendModules {
    modules = [{
      envy.software.sources."homebrew:${tap}" = {
        label = "Vendor tools";
        ecosystem = "homebrew";
        type = "tap";
        platforms = [ "darwin" ];
        scope = "system";
        parameters = {
          inherit tap;
          trustedFormulae = [ "reviewed" "excluded" ];
        };
      };
      envy.darwin.software.homebrew = {
        repositories.include = [ tap ];
        formulae.include = [ "${tap}/reviewed" "${tap}/sibling" "${tap}/excluded" "other/tools/reviewed" ];
        formulae.exclude = [ "${tap}/excluded" ];
      };
    }];
  };
  withoutTap = host.extendModules {
    modules = [{ envy.darwin.software.homebrew.repositories.exclude = [ tap ]; }];
  };
  lines = configuration: lib.splitString "\n" configuration.config.homebrew.brewfile;
  fixture = lines host;
  reviewed = ''brew "${tap}/reviewed"'';
  trusted = reviewed + ", trusted: true";
in
assert builtins.elem trusted fixture;
assert !(builtins.elem reviewed fixture);
assert builtins.elem ''tap "${tap}"'' fixture;
assert builtins.elem ''brew "${tap}/sibling"'' fixture;
assert builtins.elem ''brew "other/tools/reviewed"'' fixture;
assert !(lib.hasInfix "${tap}/excluded" host.config.homebrew.brewfile);
assert builtins.elem reviewed (lines withoutTap);
assert !(builtins.elem trusted (lines withoutTap));
true
