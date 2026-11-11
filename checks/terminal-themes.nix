{ lib, darwinConfigurations, homeConfigurations }:

let
  hasPackage = name: config: builtins.elem name (map lib.getName config.home.packages);
  homeUser = config: config.home-manager.users.${config.envy.user.name};
  hasConfig = name: config: builtins.hasAttr name config.xdg.configFile;
  profileNames = config:
    if builtins.hasAttr "iterm2-tokyo-night-profile" config.home.file then
      map (profile: profile.Name)
        (builtins.fromJSON (builtins.unsafeDiscardStringContext
          config.home.file.iterm2-tokyo-night-profile.text)).Profiles
    else [ ];
  checkShell = config:
    let
      init = config.programs.zsh.initContent;
      lines = lib.splitString "\n" init;
      position = text: lib.lists.findFirstIndex (line: lib.hasInfix text line) (-1) lines;
      fzfSelected = hasPackage "fzf" config;
      zoxideSelected = hasPackage "zoxide" config;
      atuinSelected = hasPackage "atuin" config;
    in
      !config.programs.fzf.enable
      && !config.programs.zoxide.enable
      && config.envy.software.nix.packages.references.fzf == "nix:fzf"
      && config.envy.software.nix.packages.references.zoxide == "nix:zoxide"
      && !config.programs.atuin.enable
      && config.envy.software.nix.packages.references.atuin == "nix:atuin"
      && hasConfig "atuin/config.toml" config == atuinSelected
      && hasConfig "atuin/themes/envy-night.toml" config == atuinSelected
      && lib.hasInfix "/bin/atuin init zsh --disable-up-arrow" init == atuinSelected
      && (!atuinSelected || (
        (!fzfSelected || position "/bin/fzf --zsh" < position "/bin/atuin init zsh")
        && position "/bin/atuin init zsh" < position "zsh-syntax-highlighting.zsh"
        && (!fzfSelected || position "/bin/atuin init zsh" < position "/share/fzf-tab/fzf-tab.plugin.zsh")))
      && !(builtins.elem "z" config.programs.zsh.oh-my-zsh.plugins)
      && lib.hasInfix "/bin/fzf --zsh" init == fzfSelected
      && lib.hasInfix "/bin/zoxide init zsh" init == zoxideSelected
      && lib.hasInfix "/share/fzf-tab/fzf-tab.plugin.zsh" init == fzfSelected
      && (!fzfSelected || (
        position "oh-my-zsh.sh" < position "/bin/fzf --zsh"
        && position "/bin/fzf --zsh" < position "/share/fzf-tab/fzf-tab.plugin.zsh"
        && position "/share/fzf-tab/fzf-tab.plugin.zsh" < position "zsh-syntax-highlighting.zsh"));
  checkHome = config:
    let
      batSelected = hasPackage "bat" config;
      btopSelected = hasPackage "btop" config;
      deltaSelected = hasPackage "delta" config;
      themeCacheNeeded = batSelected || deltaSelected;
    in
      checkShell config
      && !config.programs.bat.enable
      && builtins.readFile config.home.file.".p10k.zsh".source == builtins.readFile ../files/zsh/p10k.zsh
      && !config.programs.delta.enable
      && config.envy.software.nix.packages.references.delta == "nix:delta"
      && hasConfig "delta/config" config == deltaSelected
      && ((config.programs.git.settings.pager.diff or "") != "") == deltaSelected
      && (!deltaSelected || (
        lib.hasSuffix "/bin/delta" config.programs.git.settings.pager.diff
        && config.programs.git.settings.pager.log == config.programs.git.settings.pager.diff
        && config.programs.git.settings.pager.show == config.programs.git.settings.pager.diff
        && lib.any (include: include.path == "${config.xdg.configHome}/delta/config") config.programs.git.includes))
      && config.programs.btop.package == null
      && config.programs.btop.enable == btopSelected
      && hasConfig "bat/config" config == batSelected
      && hasConfig "bat/themes/tokyonight_night.tmTheme" config == themeCacheNeeded
      && builtins.hasAttr "batCache" config.home.activation == themeCacheNeeded
      && hasConfig "btop/btop.conf" config == btopSelected
      && hasConfig "btop/themes/tokyonight_night.theme" config == btopSelected
      && hasConfig "alacritty/alacritty.toml" config == config.programs.alacritty.enable
      && (!config.programs.alacritty.enable || (
        config.programs.alacritty.package == null
        && config.programs.alacritty.settings.font.normal.family == "Maple Mono NF CN"
        && config.programs.alacritty.settings.font.size == 12.0));
  checkDarwin = configuration:
    let
      username = configuration.config.envy.user.name;
      home = homeUser configuration.config;
      excluded = homeUser (configuration.extendModules {
        modules = [{
          home-manager.users.${username}.envy.software.nix.packages.exclude =
            lib.mkAfter [ "bat" "btop" "tmux" "fzf" "zoxide" "delta" "atuin" ];
        }];
      }).config;
      withoutBat = homeUser (configuration.extendModules {
        modules = [{ home-manager.users.${username}.envy.software.nix.packages.exclude = lib.mkAfter [ "bat" ]; }];
      }).config;
      local = homeUser (configuration.extendModules {
        modules = [{ home-manager.users.${username}.envy.vscode.mode = lib.mkForce "local"; }];
      }).config;
      localProfile = local.programs.vscode.profiles.default or { };
    in
      checkHome home
      && checkHome excluded
      && checkHome withoutBat
      && !hasPackage "bat" withoutBat
      && !hasPackage "bat" excluded
      && !hasPackage "btop" excluded
      && !hasPackage "tmux" excluded
      && !hasPackage "fzf" excluded
      && !hasPackage "zoxide" excluded
      && !hasPackage "delta" excluded
      && !hasPackage "atuin" excluded
      && !(builtins.hasAttr ".tmux.conf.local" excluded.home.file)
      && profileNames excluded == builtins.filter (name: name != "BtopDashboard") (profileNames home)
      && (home.envy.vscode.mode != "remote"
        || !(builtins.hasAttr "default" home.programs.vscode.profiles))
      && (!local.programs.vscode.enable || (
        localProfile.userSettings."workbench.colorTheme" == "Tokyo Night"
        && builtins.elem "vscode-extension-enkia-tokyo-night" (map lib.getName localProfile.extensions)));
in
lib.all checkDarwin (builtins.attrValues darwinConfigurations)
&& lib.all (configuration: checkHome configuration.config) (builtins.attrValues homeConfigurations)
