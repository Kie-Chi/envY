{
  upstream = {
    nix = {
      substituters = [ "https://cache.nixos.org/" ];
      extraSubstituters = [ "https://cache.thalheim.io" ];
      extraTrustedPublicKeys = [
        "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
      ];
    };
    npm.registry = "https://registry.npmjs.org/";
    python.index = "https://pypi.org/simple";
    go.proxy = "https://proxy.golang.org,direct";
    rust = {
      distServer = "https://static.rust-lang.org";
      updateRoot = "https://static.rust-lang.org/rustup";
      cargoIndex = "sparse+https://index.crates.io/";
    };
    maven.repository = "https://repo1.maven.org/maven2";
    conda = {
      defaultChannels = [
        "https://repo.anaconda.com/pkgs/main"
        "https://repo.anaconda.com/pkgs/r"
      ];
      condaForge = "https://conda.anaconda.org/conda-forge";
    };
    flakeSourceMirror = null;
    homebrew = {
      installerUrl = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh";
      apiDomain = "https://formulae.brew.sh/api";
      bottleDomain = "https://ghcr.io/v2/homebrew/core";
      brewGitRemote = "https://github.com/Homebrew/brew";
      coreGitRemote = "https://github.com/Homebrew/homebrew-core";
    };
    apt = {
      ubuntu = "https://archive.ubuntu.com/ubuntu";
      ubuntuPorts = "https://ports.ubuntu.com/ubuntu-ports";
      debian = "https://deb.debian.org/debian";
      debianSecurity = "https://security.debian.org/debian-security";
    };
    dockerInstallerMirror = null;
    probes = {
      common = [
        { name = "Nix cache"; url = "https://cache.nixos.org/nix-cache-info"; }
        { name = "sops-nix cache"; url = "https://cache.thalheim.io/nix-cache-info"; }
        { name = "npm registry"; url = "https://registry.npmjs.org/-/ping"; }
        { name = "PyPI index"; url = "https://pypi.org/simple/pip/"; }
        { name = "Go proxy"; url = "https://proxy.golang.org/golang.org/x/text/@v/list"; }
        { name = "Rust dist"; url = "https://static.rust-lang.org/dist/channel-rust-stable.toml.sha256"; }
        { name = "Cargo index"; url = "https://index.crates.io/config.json"; }
        { name = "Maven central"; url = "https://repo1.maven.org/maven2/org/apache/maven/maven-core/maven-metadata.xml"; }
        { name = "Conda defaults"; url = "https://repo.anaconda.com/pkgs/main/noarch/repodata.json.zst"; }
      ];
      darwin = [
        { name = "Homebrew API"; url = "https://formulae.brew.sh/api/formula/curl.json"; }
      ];
      linux = [
        { name = "Ubuntu archive"; url = "https://archive.ubuntu.com/ubuntu/dists/noble/InRelease"; }
        { name = "Debian archive"; url = "https://deb.debian.org/debian/dists/bookworm/InRelease"; }
        { name = "Docker installer"; url = "https://get.docker.com"; }
      ];
    };
  };

  china = {
    nix = {
      # USTC mirrors cache.nixos.org artifacts without replacing Nix signature checks.
      substituters = [
        "https://mirrors.ustc.edu.cn/nix-channels/store"
        "https://cache.nixos.org/"
      ];
      extraSubstituters = [ "https://cache.thalheim.io" ];
      extraTrustedPublicKeys = [
        "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
      ];
    };
    npm.registry = "https://registry.npmmirror.com";
    python.index = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple";
    go.proxy = "https://goproxy.cn,direct";
    rust = {
      distServer = "https://rsproxy.cn";
      updateRoot = "https://rsproxy.cn/rustup";
      cargoIndex = "sparse+https://rsproxy.cn/index/";
    };
    maven.repository = "https://maven.aliyun.com/repository/public";
    conda = {
      defaultChannels = [
        "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main"
        "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r"
        "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2"
      ];
      condaForge = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge";
    };
    flakeSourceMirror = "https://gh-proxy.com/";
    homebrew = {
      installerUrl = "https://mirrors.ustc.edu.cn/misc/brew-install.sh";
      apiDomain = "https://mirrors.ustc.edu.cn/homebrew-bottles/api";
      bottleDomain = "https://mirrors.ustc.edu.cn/homebrew-bottles";
      brewGitRemote = "https://mirrors.ustc.edu.cn/brew.git";
      # USTC no longer publishes homebrew-core.git. Modern Homebrew uses the
      # mirrored JSON API by default; explicit core Git use stays upstream.
      coreGitRemote = "https://github.com/Homebrew/homebrew-core";
    };
    apt = {
      ubuntu = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu";
      ubuntuPorts = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports";
      debian = "https://mirrors.tuna.tsinghua.edu.cn/debian";
      debianSecurity = "https://mirrors.tuna.tsinghua.edu.cn/debian-security";
    };
    dockerInstallerMirror = "Aliyun";
    probes = {
      common = [
        { name = "Nix cache"; url = "https://mirrors.ustc.edu.cn/nix-channels/store/nix-cache-info"; }
        { name = "sops-nix cache"; url = "https://cache.thalheim.io/nix-cache-info"; }
        { name = "npm registry"; url = "https://registry.npmmirror.com/-/ping"; }
        { name = "PyPI index"; url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/pip/"; }
        { name = "Go proxy"; url = "https://goproxy.cn/golang.org/x/text/@v/list"; }
        { name = "Rust dist"; url = "https://rsproxy.cn/dist/channel-rust-stable.toml.sha256"; }
        { name = "Cargo index"; url = "https://rsproxy.cn/index/config.json"; }
        { name = "Maven central"; url = "https://maven.aliyun.com/repository/public/org/apache/maven/maven-core/maven-metadata.xml"; }
        { name = "Conda defaults"; url = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/noarch/repodata.json.zst"; }
      ];
      darwin = [
        { name = "Homebrew installer"; url = "https://mirrors.ustc.edu.cn/misc/brew-install.sh"; }
        { name = "Homebrew API"; url = "https://mirrors.ustc.edu.cn/homebrew-bottles/api/formula/curl.json"; }
      ];
      linux = [
        { name = "Ubuntu archive"; url = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/dists/noble/InRelease"; }
        { name = "Debian archive"; url = "https://mirrors.tuna.tsinghua.edu.cn/debian/dists/bookworm/InRelease"; }
        { name = "Docker mirror"; url = "https://mirrors.aliyun.com/docker-ce/linux/ubuntu/dists/noble/InRelease"; }
      ];
    };
  };
}
