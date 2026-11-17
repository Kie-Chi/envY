{ lib }:

let
  home = "/tmp/storage-test-owner";
  evaluate = machinePlatform: extra: (lib.evalModules {
    specialArgs = { inherit machinePlatform; };
    modules = [
      ({ lib, ... }: {
        options.assertions = lib.mkOption {
          type = lib.types.listOf lib.types.attrs;
          default = [ ];
        };
        options.envy.user.home = lib.mkOption {
          type = lib.types.str;
          default = home;
        };
      })
      ../modules/storage/options.nix
      extra
    ];
  }).config;
  valid = cfg: lib.all (entry: entry.assertion) cfg.assertions;
  rule = cfg: id: lib.findFirst (entry: entry.id == id) null cfg.envy.storage.rules.effective;
  darwin = evaluate "darwin" { };
  linux = evaluate "linux" { };
  changed = evaluate "darwin" {
    envy.storage.rules = {
      exclude = [ "feishu-web-cache-aha" ];
      include.old-installers.minAgeDays = 60;
      include.machine-cache = {
        path = home + "/custom/cache";
        action = "quarantine";
      };
    };
  };
  nativeGo = evaluate "darwin" {
    envy.storage.maintenance.go = true;
    envy.storage.rules.exclude = [ "go-build-cache" ];
  };
  moduleGo = evaluate "darwin" { envy.storage.maintenance.go-modcache = true; };
  mounted = evaluate "darwin" {
    envy.storage.archive = {
      repository = "/Volumes/archive/restic";
      mountSource = "//fixture@nas/share";
    };
  };
  missingSource = evaluate "darwin" {
    envy.storage.archive.repository = "/Volumes/archive/restic";
  };
  straySource = repository: evaluate "darwin" {
    envy.storage.archive = {
      inherit repository;
      mountSource = "//fixture@nas/share";
    };
  };
  unsafeSource = mountSource: evaluate "darwin" {
    envy.storage.archive = {
      repository = "/Volumes/archive/restic";
      inherit mountSource;
    };
  };
  sftp = evaluate "darwin" {
    envy.storage.archive.repository = "sftp:fixture@nas:/archive/restic";
  };
  unknown = evaluate "darwin" { envy.storage.rules.exclude = [ "not-a-rule" ]; };
  invalid = evaluate "darwin" {
    envy.storage.rules.include.bad-name = {
      path = home + "/cache";
      action = "quarantine";
      targets = "dirs";
      extensions = [ "zip" ];
    };
  };
  conflict = builtins.tryEval (lib.deepSeq (evaluate "darwin" {
    imports = [
      { envy.storage.rules.include.old-installers.minAgeDays = 60; }
      { envy.storage.rules.include.old-installers.minAgeDays = 90; }
    ];
  }).envy.storage.rules.effective true);
  legacy = builtins.tryEval (lib.deepSeq (evaluate "darwin" {
    envy.storage.rules = [ ];
  }).envy.storage.rules.effective true);
  updateIds = [
    "feishu-update" "codex-sparkle" "vscode-shipit"
    "wechat-sparkle" "zotero-update-archive" "zotero-update-staging"
  ];
  updatePolicy = evaluate "darwin" {
    envy.storage.rules = {
      exclude = [ "wechat-sparkle" ];
      include.zotero-update-archive.minAgeDays = 45;
    };
  };
  pruneIds = [
    "vscode-gpu-cache" "codex-shader-cache" "chrome-shader-cache"
    "feishu-shader-cache" "feishu-iron-shader-cache" "steam-shader-cache"
  ];
  prunePolicy = evaluate "darwin" {
    envy.storage.rules = {
      exclude = pruneIds;
    };
  };
  revokedPrune = evaluate "darwin" {
    envy.storage.rules.include.vscode-gpu-cache.prune = false;
  };
  grants = lib.concatMap (entry: entry.protectedOverrides) changed.envy.storage.rules.effective;
in
assert valid darwin && valid linux && valid changed;
assert valid prunePolicy && valid revokedPrune;
assert lib.all (id:
  let entry = rule darwin id;
  in entry != null && entry.prune && entry.action == "quarantine"
    && entry.targets == "dirs" && entry.depth == 1 && entry.names != [ ]
    && entry.recursiveAge && entry.optionalRoot && entry.processes != [ ]
) pruneIds;
assert lib.all (entry: entry.prune == builtins.elem entry.id pruneIds)
  darwin.envy.storage.rules.effective;
assert lib.all (entry: !entry.prune) linux.envy.storage.rules.effective;
assert !(rule changed "machine-cache").prune;
assert lib.all (id: rule prunePolicy id == null) pruneIds;
assert lib.all (entry: !entry.prune) prunePolicy.envy.storage.rules.effective;
assert !(rule revokedPrune "vscode-gpu-cache").prune;
assert (rule revokedPrune "vscode-gpu-cache").overriddenFields == [ "prune" ];
assert valid mounted && valid sftp;
assert darwin.envy.storage.archive.mountSource == "";
assert !valid missingSource;
assert !valid (straySource "") && !valid (straySource "sftp:fixture@nas:/archive/restic");
assert lib.all (source: !valid (unsafeSource source)) [
  "//fixture:password@nas/share"
  "smb://fixture:password@nas/share"
  "https://fixture:password@nas/share"
  "//fixture@nas/share\n"
  "//fixture@nas/sha\tre"
  ("//fixture@nas/share" + builtins.fromJSON ''"\u007f"'')
];
assert rule darwin "old-installers" != null;
assert (rule darwin "old-installers").source == "default:common";
assert (rule darwin "old-installers").overriddenFields == [ ];
assert (rule changed "old-installers").minAgeDays == 60;
assert (rule changed "old-installers").extensions == (rule darwin "old-installers").extensions;
assert (rule changed "old-installers").overriddenFields == [ "minAgeDays" ];
assert (rule changed "machine-cache").source == "machine";
assert !(rule changed "machine-cache").optionalRoot;
assert rule changed "feishu-web-cache-aha" == null;
assert !(lib.any (path: lib.hasInfix "LarkShell/aha" path) grants);
assert lib.all (entry: lib.hasPrefix home entry.path) darwin.envy.storage.rules.effective;
assert lib.all (entry: lib.hasPrefix home entry.path) linux.envy.storage.rules.effective;
assert rule linux "feishu-update" == null && rule linux "codex-sparkle" == null;
assert (rule linux "go-build-cache").path == home + "/.cache/go-build";
assert (rule darwin "go-build-cache").path == home + "/Library/Caches/go-build";
assert lib.all (id: rule linux id == null) updateIds;
assert lib.all (id:
  let entry = rule darwin id;
  in entry != null && entry.source == "default:darwin"
    && entry.action == "quarantine" && entry.minAgeDays == 14
    && entry.depth == (if id == "wechat-sparkle" then 2 else 1) && entry.optionalRoot
    && (entry.targets != "dirs" || entry.recursiveAge)
) updateIds;
assert (rule darwin "wechat-sparkle").targets == "files";
assert (rule darwin "wechat-sparkle").extensions == [ "dmg" ];
assert rule updatePolicy "wechat-sparkle" == null;
assert (rule updatePolicy "zotero-update-archive").minAgeDays == 45;
assert (rule updatePolicy "zotero-update-archive").names == [ "update.mar" ];
assert (rule updatePolicy "zotero-update-archive").overriddenFields == [ "minAgeDays" ];
assert (rule darwin "zotero-update-staging").names == [ "Updated.app" ];
assert (rule darwin "feishu-update").protectedOverrides == [
  (home + "/Library/Application Support/LarkShell/update/update.noindex")
  (home + "/Library/Application Support/LarkShell/update/update_downloading")
];
assert !darwin.envy.storage.maintenance.go-modcache;
assert !linux.envy.storage.maintenance.go-modcache;
assert moduleGo.envy.storage.maintenance.go-modcache;
assert rule moduleGo "go-build-cache" != null;
assert rule nativeGo "go-build-cache" == null;
assert nativeGo.envy.storage.rules.exclude == [ "go-build-cache" ];
assert !valid unknown && !valid invalid;
assert !conflict.success && !legacy.success;
true
