{ inputs }:

let
  pkgs = inputs.nixpkgs.legacyPackages.x86_64-linux;
  lib = import (inputs.home-manager + "/modules/lib/stdlib-extended.nix") pkgs.lib;
  inherit (lib) mkOption types;
  declaration = import ../modules/envy/options.nix { inherit lib pkgs; };
  evaluateHabit = habit: (lib.evalModules {
    modules = [
      { options.habits = declaration.options.envy.machine.habits; }
      { habits = [ habit ]; }
    ];
  }).config.habits;
  binding = {
    id = "fixture-binding";
    label = "Fixture binding";
    gesture = "F12";
    semantic = "Fixture action";
    context = "fixture";
    backend = "fixture";
    binding = "F12";
    ownership = "declarative";
  };
  preference = builtins.removeAttrs binding [ "gesture" ] // { kind = "preference"; };
  accepts = value: (builtins.tryEval (builtins.deepSeq (evaluateHabit value) true)).success;
  readback = expected: { adapter = "fixture"; inherit expected; };
  valid = [
    binding
    preference
    (preference // { gesture = null; })
    (preference // { gesture = "F1"; })
  ] ++ map (expected: preference // { readback = readback expected; }) [
    true false 0 2 "" "F12"
    { names = [ "English (US)" ]; current_idx = 0; }
    { names = [ "English (US)" ]; current_idx = 255; }
  ];
  invalid = [
    (builtins.removeAttrs binding [ "gesture" ])
    (binding // { gesture = null; })
    (binding // { gesture = ""; })
    (binding // { gesture = 12; })
    (preference // { gesture = ""; })
    (preference // { gesture = 12; })
    (preference // { kind = "other"; })
    (preference // { readback = {}; })
    (preference // { readback = { adapter = ""; expected = true; }; })
    (preference // { readback = { adapter = "fixture"; }; })
    (preference // { readback = { expected = true; }; })
    (preference // { readback = readback true // { command = "echo unsafe"; }; })
  ] ++ map (expected: preference // { readback = readback expected; }) [
    null 1.5 [] {} { arbitrary = true; }
    { names = [ "English (US)" ]; }
    { current_idx = 0; }
    { names = [ "English (US)" ]; current_idx = -1; }
    { names = [ "English (US)" ]; current_idx = 256; }
    { names = [ "English (US)" ]; current_idx = true; }
    { names = [ "English (US)" ]; current_idx = 0; command = "echo unsafe"; }
    { names = [ "" ]; current_idx = 0; }
  ];
  oldBinding = builtins.head (evaluateHabit binding);
  newPreference = builtins.head (evaluateHabit preference);

  # Evaluate owning modules without activation or unrelated machine policy.
  options = config: {
    envy = {
      machine.habits = declaration.options.envy.machine.habits;
      habits = declaration.options.envy.habits;
      software = declaration.options.envy.software;
      linux.option = mkOption { type = types.str; default = "desktop"; };
      linux.desktop = mkOption { type = types.str; default = "all"; };
      user.name = mkOption { type = types.str; default = "habit-fixture"; };
    };
    dconf.settings = mkOption { type = types.attrsOf (types.attrsOf types.anything); default = {}; };
    xdg.configHome = mkOption { type = types.str; default = "${config.home.homeDirectory}/.config"; };
    xdg.configFile = mkOption {
      type = (import (inputs.home-manager + "/modules/lib/file-type.nix") {
        inherit lib pkgs;
        inherit (config.home) homeDirectory;
      }).fileType "xdg.configFile" "XDG configuration home" config.xdg.configHome;
      default = {};
    };
    home.homeDirectory = mkOption { type = types.str; default = "/home/habit-fixture"; };
    home.profileDirectory = mkOption { type = types.str; default = "/home/habit-fixture/.nix-profile"; };
    home.activation = mkOption { type = types.attrs; default = {}; };
    gtk = mkOption { type = types.attrs; default = {}; };
    services = mkOption { type = types.attrs; default = {}; };
    programs = mkOption { type = types.attrs; default = {}; };
    nixpkgs = mkOption { type = types.attrs; default = {}; };
    nix = mkOption { type = types.attrs; default = {}; };
    system = {
      stateVersion = mkOption { type = types.int; };
      primaryUser = mkOption { type = types.str; };
      defaults = {
        dock = mkOption { type = types.attrs; };
        finder = mkOption { type = types.attrs; };
        NSGlobalDomain = {
          KeyRepeat = mkOption { type = types.nullOr types.int; default = null; };
          "com.apple.keyboard.fnState" = mkOption { type = types.nullOr types.bool; default = null; };
          "com.apple.mouse.tapBehavior" = mkOption { type = types.int; };
        };
      };
    };
  };
  evaluate = module: extra: (lib.evalModules {
    specialArgs = {
      inherit pkgs;
      sys.config.activation = value: value;
    };
    modules = [ ({ config, ... }: { options = options config; }) module extra ];
  }).config;
  habit = id: cfg: lib.findFirst (entry: entry.id == id) null cfg.envy.machine.habits;
  ids = cfg: map (entry: entry.id) cfg.envy.machine.habits;
  darwin = evaluate ../modules/desktops/darwin/base.nix {};
  darwinOverride = evaluate ../modules/desktops/darwin/base.nix {
    system.defaults.NSGlobalDomain = {
      KeyRepeat = lib.mkForce 7;
      "com.apple.keyboard.fnState" = lib.mkForce false;
    };
  };
  darwinNull = evaluate ../modules/desktops/darwin/base.nix {
    system.defaults.NSGlobalDomain = {
      KeyRepeat = lib.mkForce null;
      "com.apple.keyboard.fnState" = lib.mkForce null;
    };
  };
  darwinNullRepeat = evaluate ../modules/desktops/darwin/base.nix {
    system.defaults.NSGlobalDomain.KeyRepeat = lib.mkForce null;
  };
  darwinNullFn = evaluate ../modules/desktops/darwin/base.nix {
    system.defaults.NSGlobalDomain."com.apple.keyboard.fnState" = lib.mkForce null;
  };
  gnomePath = "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake";
  gnome = evaluate ../modules/desktops/linux/gnome.nix {};
  gnomeOnly = evaluate ../modules/desktops/linux/gnome.nix { envy.linux.desktop = "gnome"; };
  gnomeOverride = evaluate ../modules/desktops/linux/gnome.nix {
    dconf.settings.${gnomePath}.binding = lib.mkForce "<Alt>F10";
  };
  gnomeGesture = evaluate ../modules/desktops/linux/gnome.nix {
    envy.habits.terminalScratchpad.gesture = "F9";
  };
  gnomeServer = evaluate ../modules/desktops/linux/gnome.nix { envy.linux.option = "server"; };
  gnomeNone = evaluate ../modules/desktops/linux/gnome.nix { envy.linux.desktop = "none"; };
  niri = evaluate ../modules/desktops/linux/niri.nix {};
  niriOnly = evaluate ../modules/desktops/linux/niri.nix { envy.linux.desktop = "niri"; };
  niriCustom = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".text = lib.mkForce ''
      input { keyboard { xkb { layout "de"; }; }; }
    '';
  };
  customNiriSource = pkgs.writeText "niri-custom-de.kdl" ''
    input { keyboard { xkb { layout "de"; }; }; }
  '';
  niriSource = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".source = customNiriSource;
  };
  niriDisabled = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".enable = false;
  };
  niriRetargeted = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".target = "niri/alternate.kdl";
  };
  niriNullText = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl" = {
      text = lib.mkForce null;
      source = customNiriSource;
    };
  };
  niriExecutable = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".executable = true;
  };
  niriSameSource = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configFile."niri/config.kdl".source = niri.xdg.configFile."niri/config.kdl".source;
  };
  niriConfigHome = evaluate ../modules/desktops/linux/niri.nix {
    xdg.configHome = "/home/habit-fixture/alternate-config";
  };
  niriManual = [ niriCustom niriSource niriDisabled niriRetargeted niriNullText ];
  niriExcluded = evaluate ../modules/desktops/linux/niri.nix { envy.software.nix.packages.exclude = [ "niri" ]; };
  niriServer = evaluate ../modules/desktops/linux/niri.nix { envy.linux.option = "server"; };
  niriNone = evaluate ../modules/desktops/linux/niri.nix { envy.linux.desktop = "none"; };
  niriLayout = habit "keyboard-layout" niri;
  niriBindings = builtins.filter (entry: entry.kind == "binding") niri.envy.machine.habits;
in
assert lib.all accepts valid;
assert lib.all (value: !(accepts value)) invalid;
assert oldBinding.kind == "binding" && oldBinding.readback == null;
assert builtins.intersectAttrs binding oldBinding == binding;
assert oldBinding.note == "" && oldBinding.requirements == [];
assert newPreference.gesture == null && newPreference.readback == null;
assert (habit "keyboard-repeat" darwin).readback == { adapter = "darwin.key-repeat"; expected = 2; };
assert (habit "function-key-mode" darwin).readback == { adapter = "darwin.fn-state"; expected = true; };
assert (habit "keyboard-repeat" darwinOverride).readback.expected == 7;
assert (habit "function-key-mode" darwinOverride).readback.expected == false;
assert darwinNull.envy.machine.habits == [];
assert ids darwinNullRepeat == [ "function-key-mode" ];
assert ids darwinNullFn == [ "keyboard-repeat" ];
assert lib.all (entry: entry.kind == "preference" && entry.gesture == null
  && entry.context == "darwin" && entry.backend == "defaults"
  && entry.ownership == "declarative") darwin.envy.machine.habits;
assert (habit "terminal-scratchpad" gnome).readback == {
  adapter = "linux.gnome-scratchpad";
  expected = gnome.dconf.settings.${gnomePath}.binding;
};
assert gnomeOnly.envy.machine.habits == gnome.envy.machine.habits;
assert (habit "terminal-scratchpad" gnomeOverride).readback.expected == "<Alt>F10";
assert (habit "terminal-scratchpad" gnomeGesture).readback.expected == "F9";
assert (habit "terminal-scratchpad" gnomeGesture).gesture == "F9"
  && gnomeGesture.dconf.settings.${gnomePath}.binding == "F9";
assert gnomeServer.envy.machine.habits == [] && gnomeNone.envy.machine.habits == [];
assert niriOnly.envy.machine.habits == niri.envy.machine.habits;
assert niriLayout.readback == {
  adapter = "linux.niri-keyboard-layout";
  expected = { names = [ "English (US)" ]; current_idx = 0; };
};
assert niriLayout.kind == "preference" && niriLayout.gesture == null
  && niriLayout.context == "niri" && niriLayout.ownership == "declarative";
assert niriLayout.requirements == [{ group = "nix.user.package"; item = "niri"; }];
assert lib.hasInfix ''layout "us"'' niri.xdg.configFile."niri/config.kdl".text;
assert map (entry: entry.id) niriBindings == [ "terminal-scratchpad" "global-launcher" ];
assert lib.all (entry: entry.readback == null && entry.gesture != "") niriBindings;
assert lib.all (cfg:
  let layout = habit "keyboard-layout" cfg;
  in layout.readback == null
    && lib.hasInfix "manual verification" layout.note
    && !(lib.hasInfix "English (US)" layout.semantic)) niriManual;
assert niriSource.xdg.configFile."niri/config.kdl".text == niri.xdg.configFile."niri/config.kdl".text;
assert toString niriSource.xdg.configFile."niri/config.kdl".source == toString customNiriSource;
assert toString niriSource.xdg.configFile."niri/config.kdl".source
  != toString niri.xdg.configFile."niri/config.kdl".source;
assert !niriDisabled.xdg.configFile."niri/config.kdl".enable;
assert niriRetargeted.xdg.configFile."niri/config.kdl".target == ".config/niri/alternate.kdl";
assert niriNullText.xdg.configFile."niri/config.kdl".text == null;
assert lib.all (cfg: (habit "keyboard-layout" cfg).readback == niriLayout.readback)
  [ niriExecutable niriSameSource niriConfigHome ];
assert toString niriExecutable.xdg.configFile."niri/config.kdl".source
  != toString niri.xdg.configFile."niri/config.kdl".source;
assert niriConfigHome.xdg.configFile."niri/config.kdl".target == "alternate-config/niri/config.kdl";
assert lib.hasInfix ''layout "de"'' niriCustom.xdg.configFile."niri/config.kdl".text;
assert lib.all (id: (habit id niriCustom).readback == null) [ "terminal-scratchpad" "global-launcher" ];
assert niriExcluded.envy.machine.habits == []
  && niriServer.envy.machine.habits == [] && niriNone.envy.machine.habits == [];
true
