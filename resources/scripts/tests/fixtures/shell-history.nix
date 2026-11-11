let
  f = builtins.getFlake ("path:" + toString ../../../..);
  lib = f.inputs.nixpkgs.lib;
  primary = builtins.head (builtins.attrValues f.darwinConfigurations);
  user = primary.config.envy.user.name;
  home = c: c.config.home-manager.users.${user};
  h = home primary;
  excluded = names: home (primary.extendModules {
    modules = [{ home-manager.users.${user}.envy.software.nix.packages.exclude = lib.mkAfter names; }];
  });
  summary = c: {
    counts = lib.genAttrs [ "delta" "atuin" "bat" "fzf" ]
      (name: builtins.length (builtins.filter (p: lib.getName p == name) c.home.packages));
    deltaConfig = builtins.hasAttr "delta/config" c.xdg.configFile;
    atuinConfig = builtins.hasAttr "atuin/config.toml" c.xdg.configFile;
    batConfig = builtins.hasAttr "bat/config" c.xdg.configFile;
    themeCache = builtins.hasAttr "batCache" c.home.activation;
    pager = c.programs.git.settings.pager or {};
    initContent = c.programs.zsh.initContent;
    daemon = c.programs.atuin.daemon.enable;
    service = builtins.hasAttr "atuin-daemon" c.systemd.user.services
      || builtins.hasAttr "atuin-daemon" c.launchd.agents;
  };
in {
  selected = summary h;
  none = summary (excluded [ "delta" "atuin" ]);
  withoutBat = summary (excluded [ "bat" ]);
  withoutFzf = summary (excluded [ "fzf" ]);
  deltaConfig = h.xdg.configFile."delta/config".text;
  atuinConfig = h.xdg.configFile."atuin/config.toml".text;
  atuinTheme = h.xdg.configFile."atuin/themes/envy-night.toml".text;
  theme = toString h.xdg.configFile."bat/themes/tokyonight_night.tmTheme".source;
  cacheScript = h.home.activation.batCache.data;
  configHome = h.xdg.configHome;
  cacheHome = h.xdg.cacheHome;
  p10k = toString h.home.file.".p10k.zsh".source;
}