# Package-manager endpoints exported to host sessions and amux containers.
profile: {
  npm_config_registry = profile.npm.registry;
  PIP_INDEX_URL = profile.python.index;
  UV_DEFAULT_INDEX = profile.python.index;
  GOPROXY = profile.go.proxy;
  RUSTUP_DIST_SERVER = profile.rust.distServer;
  RUSTUP_UPDATE_ROOT = profile.rust.updateRoot;
  CARGO_REGISTRIES_CRATES_IO_INDEX = profile.rust.cargoIndex;
  CARGO_REGISTRIES_CRATES_IO_PROTOCOL = "sparse";
}
