# Installation and Bootstrap

`install.sh` 是仓库之外的一层薄 bootstrap：它负责取得 checkout，然后把控制权交给仓库内的 `setup.sh`。Machine ID、import/copy 模式、软件 exclusions、非敏感配置和 secrets 仍由 setup 流程管理

## Requirements

开始前需要：

- macOS 或 Linux，具有 Bash、Git 和网络连接
- 使用远程命令时需要 `curl`；已下载脚本可以直接用 Bash 执行
- 运行交互式 setup 时需要真实终端。无 TTY 的 CI 环境应使用 `--no-setup`
- 首次安装 Nix 可能要求 `sudo`。`setup.sh` 在 `china` 模式通过清华镜像运行官方 Nix 二进制安装脚本，在 `upstream` 模式使用 Determinate Nix Installer，然后运行最小化的 setup flake app

## Remote Bootstrap

从默认 `master` clone 到 `~/.envy` 并启动交互式 setup：

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://gh-proxy.com/https://raw.githubusercontent.com/Kie-Chi/envY/master/install.sh | bash
```

默认 bootstrap mirror 是 `china`。国内模式使用 `gh-proxy.com` 获取 GitHub 仓库，失败时
回退到 GitHub 直连；可以通过 `ENVY_GIT_MIRROR_URL` 指定自己信任的 GitHub 代理。
显式使用上游环境：

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Kie-Chi/envY/master/install.sh \
  | bash -s -- --mirror upstream
```

任何 `curl | bash` 都会执行远程内容。更容易审查的方式是先下载：

```bash
curl --proto '=https' --tlsv1.2 -fL \
  https://gh-proxy.com/https://raw.githubusercontent.com/Kie-Chi/envY/master/install.sh \
  -o /tmp/envy-install.sh
less /tmp/envy-install.sh
bash /tmp/envy-install.sh
```

`master` 会持续变化。可复现安装应使用已审查的 release tag，并让 raw URL 与 clone ref 保持一致：

```bash
ENVY_RELEASE='<tag>'
curl --proto '=https' --tlsv1.2 -fsSL \
  "https://raw.githubusercontent.com/Kie-Chi/envY/$ENVY_RELEASE/install.sh" \
  | ENVY_BRANCH="$ENVY_RELEASE" bash
```

## Options

只取得仓库，不启动 setup：

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Kie-Chi/envY/master/install.sh \
  | bash -s -- --no-setup
```

本地脚本支持以下参数：

```text
--repo URL       Git repository URL
--branch NAME    要 clone 的 branch 或 tag，默认 master
--target PATH    checkout 路径，默认 $HOME/.envy
--mirror MODE    bootstrap mirror：china 或 upstream，默认 china
--no-setup       只 clone，不运行 setup.sh
```

对应环境变量是 `ENVY_REPOSITORY_URL`、`ENVY_BRANCH`、`ENVY_ROOT`、`ENVY_MIRROR` 和
`ENVY_GIT_MIRROR_URL`。`ENVY_NIX_INSTALLER_URL` 可以显式覆盖 Nix installer 下载地址；
如果该地址提供标准 Nix 安装脚本，可再设置 `ENVY_NIX_INSTALLER_ARGS='--daemon'`。
国内模式的默认 GitHub 代理是可替换的第三方 endpoint，使用前应确认其信任边界。例如：

```bash
ENVY_REPOSITORY_URL='git@github.com:Kie-Chi/envY.git' \
ENVY_BRANCH='master' \
ENVY_ROOT="$HOME/src/envy" \
ENVY_MIRROR='china' \
bash install.sh
```

`ENVY_DOTFILES` 与 `DOTFILES_DIR` 仅作为旧版本迁移输入继续被识别；新脚本、
shell 环境和文档统一使用 `ENVY_ROOT`。已有 `~/.dotfiles` checkout 应移动到
`~/.envy`，并把 remote 更新为 `git@github.com:Kie-Chi/envY.git`。bootstrap
不会自动移动已有目录，也不会覆盖其中的未提交改动。

旧机器迁移后，`~/.config/dotfiles/config.nix` 或 `/etc/dotfiles/config.nix`
可能仍引用旧目录。命名的 Linux 机器目标（例如 `linux_desktop`）的 Nix 求值不导入
这些旧配置；只有选择兼容目标 `default` 时才会导入它们。如果 apply 在
`homeConfigurations` 的 `optionalAttrs` 条件处报旧 `~/.dotfiles/config.nix`
不存在，这是旧版 flake 提前导入兼容配置的问题，需要更新 checkout 中的 `flake.nix`。
命名目标不需要为此重新创建旧 `config.nix`。

`setup.sh` 也支持 `--mirror china|upstream`。Linux 的 Nix daemon 模式会拒绝普通用户
通过 `NIX_CONFIG` 临时添加未信任的 substituter；setup 会在第一次 Nix build 前调用
`resources/scripts/nix-trust.sh`，原子维护被 `/etc/nix/nix.conf` include 的
`/etc/nix/nix.custom.conf` 中固定标记块。两种模式都信任 sops-nix 的
`cache.thalheim.io` 及固定 public key；`china` 另外信任 USTC 和官方 cache key。该 block
还使用 `extra-trusted-users` 加入当前用户，不会替换管理员已有的 `trusted-users` policy。

仅当 block 需要创建或升级时才会请求 `sudo`，更改后只重启当前 active 的 Nix daemon。
缺少 include、marker 格式损坏或没有提权能力都会明确终止 setup，避免带着实际未生效的
缓存策略继续执行大规模本地构建。envY 不会直接改写官方主配置文件的其他内容。

## Existing Checkouts

目标不存在时，bootstrap 先 clone 到 `mktemp` 创建的临时目录，再移动到目标路径。clone 失败不会留下半成品 target。

目标已经是 Git checkout 时，脚本直接使用它；不会隐式执行 `fetch`、`pull`、`reset` 或切换 branch。这样不会覆盖本地修改，但也意味着 bootstrap 不负责更新已有仓库。进入正常工作流后使用 `envy sync`。

目标存在但不是 Git checkout 时，脚本会拒绝继续且不会删除其中内容。它还拒绝把 `/` 或 `$HOME` 当作 target。

## Responsibility Boundary

完整流程是：

```text
install.sh -> clone/reuse checkout -> setup.sh -> nix run path:.#setup -> setup.py
```

- `install.sh` 只负责 repository bootstrap 和终端交接。
- `setup.sh` 只负责准备 Nix 和 setup runtime 并启动 setup。安装 Nix 后会在当前进程显式恢复 `/nix/var/nix/profiles/default/bin`，不要求重新登录才能继续 setup。
- `setup.py` 创建或选择 host、编辑 machine 配置和 software exclusions，并管理 sops secrets。
- setup 的列表、输入和变更摘要会遮罩 secret，只显示是否为空，不打印 secret 原文。

Machine policy 的 import/copy 语义及软件状态见 [machines.md](machines.md)。
bootstrap 与 apply 后的镜像职责、APT ownership 和 GitHub/source download 限制见 [mirrors.md](mirrors.md)。
