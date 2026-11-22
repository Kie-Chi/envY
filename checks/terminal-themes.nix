{ lib, darwinConfigurations, homeConfigurations }:

let
  hasPackage = name: config: builtins.elem name (map lib.getName config.home.packages);
  homeUser = config: config.home-manager.users.${config.envy.user.name};
  hasConfig = name: config: builtins.hasAttr name config.xdg.configFile;
  profiles = config:
    if builtins.hasAttr "iterm2-tokyo-night-profile" config.home.file then
      (builtins.fromJSON (builtins.unsafeDiscardStringContext
        config.home.file.iterm2-tokyo-night-profile.text)).Profiles
    else [ ];
  profileNames = config: map (profile: profile.Name) (profiles config);
  checkShell = config:
    let
      init = config.programs.zsh.initContent;
      lines = lib.splitString "\n" init;
      position = text: lib.lists.findFirstIndex (line: lib.hasInfix text line) (-1) lines;
      fzfSelected = hasPackage "fzf" config;
      zoxideSelected = hasPackage "zoxide" config;
      atuinSelected = hasPackage "atuin" config;
      direnvSelected = hasPackage "direnv" config;
      nixDirenvSelected = direnvSelected && hasPackage "nix-direnv" config;
    in
      !config.programs.direnv.enable
      && lib.hasInfix "/bin/direnv hook zsh" init == direnvSelected
      && hasConfig "direnv/lib/nix-direnv.sh" config == nixDirenvSelected
      && !config.programs.fzf.enable
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
        lib.hasInfix "zstyle ':fzf-tab:*' fzf-min-height 12" init
        && lib.hasInfix "zstyle ':fzf-tab:*' fzf-pad 4" init
        && lib.hasInfix "COLUMNS >= 100 && LINES >= 20" init
        && lib.hasInfix "--preview-window=right:55%:wrap" init
        && lib.hasInfix "--preview-window=hidden" init
        && !(lib.hasInfix "down:45%" init)
        && !(lib.hasInfix "--preview-window" (config.home.sessionVariables.FZF_DEFAULT_OPTS or ""))
        && position "oh-my-zsh.sh" < position "/bin/fzf --zsh"
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
      && config.programs.yazi.enable == hasPackage "yazi" config
      && config.programs.yazi.package == null
      && hasConfig "yazi/theme.toml" config == hasPackage "yazi" config
      && lib.hasInfix "function yy()" config.programs.zsh.initContent == hasPackage "yazi" config
      && config.programs.lazygit.enable == hasPackage "lazygit" config
      && config.programs.lazygit.package == null
      && config.programs.ghostty.package == null
      && !config.programs.ghostty.systemd.enable
      && hasConfig "ghostty/config" config == config.programs.ghostty.enable
      && (!config.programs.ghostty.enable || (
        lib.toList config.programs.ghostty.settings.font-family == [ "Maple Mono NF CN" ]
        && builtins.length config.programs.ghostty.settings.palette == 16))
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
            lib.mkAfter [ "bat" "btop" "tmux" "fzf" "zoxide" "delta" "atuin"
              "direnv" "nix-direnv" "yazi" "lazygit" "lnav" "lazydocker" "watchexec" "xh" ];
        }];
      }).config;
      withoutBat = homeUser (configuration.extendModules {
        modules = [{ home-manager.users.${username}.envy.software.nix.packages.exclude = lib.mkAfter [ "bat" ]; }];
      }).config;
      local = homeUser (configuration.extendModules {
        modules = [{ home-manager.users.${username}.envy.vscode.mode = lib.mkForce "local"; }];
      }).config;
      withoutGhostty = homeUser (configuration.extendModules {
        modules = [{ envy.darwin.software.homebrew.casks.exclude = lib.mkAfter [ "ghostty" ]; }];
      }).config;
      cmuxOnly = homeUser (configuration.extendModules {
        modules = [{
          envy.darwin.software.homebrew.casks.include = lib.mkAfter [ "cmux" ];
          envy.darwin.software.homebrew.casks.exclude = lib.mkForce [ "ghostty" ];
        }];
      }).config;
      ghosttyOnly = homeUser (configuration.extendModules {
        modules = [{
          envy.darwin.software.homebrew.casks.include = lib.mkAfter [ "ghostty" ];
          envy.darwin.software.homebrew.casks.exclude = lib.mkForce [ "cmux" ];
        }];
      }).config;
      withoutGhosttyConsumers = homeUser (configuration.extendModules {
        modules = [{ envy.darwin.software.homebrew.casks.exclude = lib.mkAfter [ "ghostty" "cmux" ]; }];
      }).config;
      withoutCmux = homeUser (configuration.extendModules {
        modules = [{ envy.darwin.software.homebrew.casks.exclude = lib.mkAfter [ "cmux" ]; }];
      }).config;
      cmuxSelected = builtins.elem "cmux" configuration.config.envy.darwin.software.homebrew.casks.effective;
      cmuxHooksSelected = cmuxSelected && !(builtins.elem "envy-cmux-hooks" home.envy.software.nix.packages.exclude);
      withoutCmuxHooks = homeUser (configuration.extendModules {
        modules = [{ home-manager.users.${username}.envy.software.nix.packages.exclude = lib.mkAfter [ "envy-cmux-hooks" ]; }];
      }).config;
      localProfile = local.programs.vscode.profiles.default or { };
    in
      checkHome home
      && checkHome excluded
      && checkHome withoutBat
      && checkHome withoutGhostty
      && withoutGhostty.programs.ghostty.enable == cmuxSelected
      && checkHome cmuxOnly
      && cmuxOnly.programs.ghostty.enable
      && checkHome ghosttyOnly
      && ghosttyOnly.programs.ghostty.enable
      && checkHome withoutGhosttyConsumers
      && !withoutGhosttyConsumers.programs.ghostty.enable
      && hasPackage "envy-cmux-hooks" home == cmuxHooksSelected
      && builtins.hasAttr "cmuxAgentHooks" home.home.activation == cmuxHooksSelected
      && builtins.hasAttr ".pi/agent/extensions/envy-cmux.ts" home.home.file
        == (cmuxHooksSelected && hasPackage "pi" home)
      && !hasPackage "envy-cmux-hooks" withoutCmuxHooks
      && !(builtins.hasAttr "cmuxAgentHooks" withoutCmuxHooks.home.activation)
      && !(builtins.hasAttr ".pi/agent/extensions/envy-cmux.ts" withoutCmuxHooks.home.file)
      && !hasPackage "envy-cmux-hooks" withoutCmux
      && !(builtins.hasAttr "cmuxAgentHooks" withoutCmux.home.activation)
      && !(builtins.hasAttr ".pi/agent/extensions/envy-cmux.ts" withoutCmux.home.file)
      && lib.all (name: !hasPackage name excluded)
        [ "direnv" "nix-direnv" "yazi" "lazygit" "lnav" "lazydocker" "watchexec" "xh" ]
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
      && lib.all (profile:
        !(builtins.elem profile.Name [ "Tokyo Night" "Quake" ]) || (
          (profile."Keyboard Map"."0xf702-0x300000" or { }) == { Action = 11; Text = "0x1"; }
          && (profile."Keyboard Map"."0xf703-0x300000" or { }) == { Action = 11; Text = "0x5"; }
        )
      ) (profiles home)
      && (home.envy.vscode.mode != "remote"
        || !(builtins.hasAttr "default" home.programs.vscode.profiles))
      && (!local.programs.vscode.enable || (
        localProfile.userSettings."workbench.colorTheme" == "Tokyo Night"
        && builtins.elem "vscode-extension-enkia-tokyo-night" (map lib.getName localProfile.extensions)));
in
lib.all checkDarwin (builtins.attrValues darwinConfigurations)
&& lib.all (configuration: checkHome configuration.config) (builtins.attrValues homeConfigurations)
