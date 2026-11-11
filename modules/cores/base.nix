###################################
#
#
#   BASE PACKAGES FOR MYSYSTEM
#
#
###################################

{ pkgs, config, lib, machinePlatform, ... }:

let
  selectedPackages = map lib.getName config.envy.software.nix.packages.effective;
  batSelected = builtins.elem "bat" selectedPackages;
  btopSelected = builtins.elem "btop" selectedPackages;
  # delta reads the same syntect theme cache as bat, even without bat on PATH.
  themeCacheNeeded = batSelected || builtins.elem "delta" selectedPackages;
  themeSource = pkgs.vimPlugins.tokyonight-nvim.src;
in
{
  envy.software.nix.packages.include = with pkgs; [
    # base
    git
    git-lfs
    tmux
    git-filter-repo

    # crypt
    git-crypt
    gnupg
    sops
    age

    # network
    curl
    wget
    wireshark

    # system
    btop
    htop
    ncdu

    # tools
    unzip
    jq
    javaPackages.compiler.openjdk21

    # opt
    ripgrep
    bat
    tree
    neovim-remote
  ] ++ lib.optionals (machinePlatform == "linux") [ rclone ];

  envy.software.nix.packages.references = {
    git = "nix:git";
    "git-lfs" = "nix:git-lfs";
    tmux = "nix:tmux";
    "git-filter-repo" = "nix:git-filter-repo";
    "git-crypt" = "nix:git-crypt";
    gnupg = "nix:gnupg";
    sops = "nix:sops";
    age = "nix:age";
    curl = "nix:curl";
    wget = "nix:wget";
    "wireshark-qt" = "nix:wireshark";
    btop = "nix:btop";
    htop = "nix:htop";
    ncdu = "nix:ncdu";
    unzip = "nix:unzip";
    jq = "nix:jq";
    ripgrep = "nix:ripgrep";
    bat = "nix:bat";
    tree = "nix:tree";
    "neovim-remote" = "nix:neovim-remote";
  } // lib.optionalAttrs (machinePlatform == "darwin") {
    "zulu-ca-jdk" = "nix:javaPackages.compiler.openjdk21";
  } // lib.optionalAttrs (machinePlatform == "linux") {
    openjdk = "nix:javaPackages.compiler.openjdk21";
    rclone = "nix:rclone";
  };

  # The locked bat module cannot disable its package installation. Keep that
  # ownership in envY and manage only the theme files and runtime cache here.
  xdg.configFile = {
    "bat/config" = lib.mkIf batSelected {
      text = ''
        --theme=tokyonight_night
      '';
    };
    "bat/themes/tokyonight_night.tmTheme" = lib.mkIf themeCacheNeeded {
      source = themeSource + "/extras/sublime/tokyonight_night.tmTheme";
    };
  };

  home.activation.batCache = lib.mkIf themeCacheNeeded (lib.hm.dag.entryAfter [ "linkGeneration" ] ''
    (
      export XDG_CONFIG_HOME=${lib.escapeShellArg config.xdg.configHome}
      export XDG_CACHE_HOME=${lib.escapeShellArg config.xdg.cacheHome}
      verboseEcho "Rebuilding bat theme cache"
      cd "${pkgs.emptyDirectory}"
      run ${lib.getExe pkgs.bat} cache --build
    )
  '');

  programs.btop = {
    enable = btopSelected;
    package = null;
    settings = {
      color_theme = "tokyonight_night";
      theme_background = false;
    };
    themes.tokyonight_night = themeSource + "/extras/btop/tokyonight_night.theme";
  };

  home.file.".config/nixpkgs/config.nix".text = ''
    {
      allowUnfree = true;
    }
  '';

  home.sessionPath = [
    "${config.envy.user.home}/.local/bin"
  ];
}
