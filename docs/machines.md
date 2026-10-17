# Cross-platform Machine Configuration

仓库采用一个 `master` 和一个 host module 对应一台设备的模型

## Host Layout

```text
.device-label
  device.machine_id = "workstation"
          |
          v
hosts/<local-platform>/workstation.nix
          |
          v
flake.nix
  |-- hosts/darwin/*.nix -> darwinConfigurations.<id>
  `-- hosts/linux/*.nix  -> homeConfigurations.<id>
```

Host 必须位于对应平台目录：

```text
hosts/darwin/work-macbook.nix
hosts/linux/workstation.nix
hosts/default.nix
```


- `hosts/default.nix` 只是可选的公共默认 module
- Host 可以 import 它、copy 它，或完全独立定义
- 最终非敏感配置始终由 `hosts/<platform>/<id>.nix` 表达
- `.device-label` 只选择本机 target 与 sops label

## Creating A Host

```bash
envy host init
envy host init work-macbook --mode import
envy host init restricted-mac --mode copy
```

`envy host init` 根据运行平台写入 `hosts/darwin` 或 `hosts/linux`。它只询问 Machine ID 和创建模式，不询问软件选择。

### Import Mode

```nix
{ ... }:

{
  imports = [ ../default.nix ];

  # `envy config refine` writes non-sensitive machine values here.
  # Add machine-specific envy.* overrides here.
}
```

这种模式会继续继承 `hosts/default.nix` 的公共默认值。

### Copy Mode

Copy 会把 `hosts/default.nix` 当前内容写入目标文件，形成不再跟随默认 policy 的快照。目标存在时默认拒绝覆盖；`--force` 会再次确认并创建 `.bak`。

## Device Metadata

`.device-label` 是 Git 忽略的 TOML：

```toml
version = 1

[device]
machine_id = "work-macbook"
sops_label = "work_macbook"
```

常用命令：

```bash
envy host select work-macbook
envy host status
envy host check
envy config check
envy config refine
```

`envy host select` and `envy host check` complete machine IDs from the current
platform directory; `envy host init --mode` completes `import` and `copy`.

`envy config refine` 可以迁移旧单行 `.device-label`、`~/.config/envy/machine`、旧 `config.nix`，以及 Darwin 的旧 `envy.darwin.proxy.*` / `envy.proxy.*`；它也会把旧的 `envy.llm.steps.*` / `envy.llm.deepseek.*` 和对应 secret 引用生成到独立的 `ENVY MANAGED AGENTS` block，然后从机器 managed block 删除旧的 LLM scalar assignments。稳态配置不再读取这些旧来源。

LLM 迁移先转换、后清理：已有 agent/endpoint/credential 的显式选择优先，只补齐旧配置缺少的迁移目标；agent block 或迁移源无效时保留旧字段。managed block 外的旧 LLM scalar assignments 也会清理，其他手写 policy 保留。`config check` 只报告待迁移或待清理状态，不写文件。

`envy config show`、其 JSON 输出以及 `envy status` 的 config snapshot 展示当前的 endpoints、credentials 和 agents，不再展示旧 LLM scalar fields。Credential 只展示逻辑名称、加密 YAML 路径和是否已设置，不输出密钥内容。

envY 使用 `path:.#<machine-id>`，所以刚创建且尚未提交的 host 文件也可以立即 check/build/apply。

## Option Boundary

命名由能力是否真正平台专有决定，不由实现文件位于哪个平台目录决定。

### Common Options

两边语义与处理一致时不加平台前缀：

| Option | Meaning |
|---|---|
| `envy.user.*` | 用户名与 Home 目录 |
| `envy.repository.path` | 当前机器的 checkout 路径 |
| `envy.git.*` | Git identity |
| `envy.environment.sessionVariables` | 非敏感的 machine session 环境变量 |
| `envy.environment.sessionPath` | 追加到 machine session `PATH` 的目录 |
| `envy.shell.zsh.aliases` | 与公共 alias 合并的 machine Zsh aliases |
| `envy.shell.zsh.initContent` | 在公共配置之后追加的 machine Zsh 初始化片段 |
| `envy.llm.*` | 非敏感 endpoint、协议和旧版兼容模型字段 |
| `envy.agents.*` | Agent adapter、endpoint、credential 引用和默认模型 |
| `envy.vscode.mode` | VS Code local/remote policy |
| `envy.software.nix.packages.include/exclude/effective` | 公共 Home Manager package 选择机制 |
| `envy.software.npm.tools.*` | 用户级 NPM tool 选择机制 |
| `envy.software.pypi.tools.*` | 由 uv 安装的用户级 PyPI tool 选择机制 |

同一公共 module 可以在 Darwin 与 Linux 上贡献不同的具体 package；只要选择机制相同，option 仍保持公共。

### Darwin-only Options

| Option | Meaning |
|---|---|
| `envy.darwin.services.mihomo.*` | Mihomo service mode and TUN policy |
| `envy.darwin.services.openssh.mode` | macOS built-in OpenSSH service mode |
| `envy.darwin.software.nix.systemPackages.*` | nix-darwin system packages |
| `envy.darwin.software.nix.fonts.*` | nix-darwin fonts |
| `envy.darwin.software.homebrew.formulae.*` | Homebrew formulae |
| `envy.darwin.software.homebrew.casks.*` | Homebrew casks |
| `envy.darwin.software.homebrew.repositories.*` | Homebrew taps |

Linux 没有 proxy option 或 proxy secret declaration。

### Linux-only Options

| Option | Meaning |
|---|---|
| `envy.linux.desktop` | `gnome`、`niri`、`all` 或 `none`。 |
| `envy.linux.option` | 当前 Linux 机器的 `desktop` / `server` 类型。 |
| `envy.linux.software.native.packages.*` | apt/dnf/pacman/zypper 原生系统 package |
| `envy.linux.software.url.artifacts.*` | 由固定公开 URL 提供的系统 artifact |

`option = "server"` 是外层能力边界：不会安装 desktop 公共包、VS Code、
GNOME、Niri，也不会生成 Fcitx、Sunshine、Waydroid 或 SwayOSD 服务和
activation。`option = "desktop"` 时，desktop selector 再决定导入 GNOME、
Niri、两者（`all`）或都不导入（`none`）；公共 desktop 工具仍由 desktop
类型统一拥有

新 option 只有在已经出现真实机器行为差异时才添加。不要为所有共享基础设施制造没有使用场景的 `enable`

### Machine Environment And Zsh

环境与 shell 扩展是跨平台的 Home Manager policy。它们应手写在 machine
文件的 managed block 外，避免 `envy config refine` 重写自由结构：

```nix
{
  envy.environment.sessionVariables = {
    EDITOR = "nvim";
    LIKED_CONDA_ENV = "research";
  };

  envy.environment.sessionPath = [
    "$HOME/.local/special-bin"
  ];

  envy.shell.zsh.aliases.work = "cd ~/work";
  envy.shell.zsh.initContent = ''
    [[ -f "$HOME/.zsh-machine-local" ]] && source "$HOME/.zsh-machine-local"
  '';
}
```

`sessionVariables` 同时进入 Home Manager session 与交互式 Zsh；同名 machine
值覆盖共享默认值。`sessionPath` 和 `initContent` 都追加在公共配置之后，aliases
保留公共键并允许 machine 覆盖同名键。这些值会在 Nix eval 时进入 store 和
evaluated manifest，因此不得包含密码、token、API key 或其他秘密；秘密仍由
sops template 在 activation 时注入。

## Managed Config Block

`envy config` 只替换 host 中的标记区块，手写 policy 保留在外部。Darwin 示例：

```nix
# BEGIN ENVY MANAGED CONFIG
envy.user.name = "chi";
envy.user.home = "/Users/chi";
envy.repository.path = "/Users/chi/.envy";
envy.git.name = "Chi";
envy.git.email = "chi@example.com";
envy.vscode.mode = "remote";
envy.darwin.services.mihomo.mode = "none";
envy.darwin.services.mihomo.tun = false;
envy.darwin.services.openssh.mode = "manual";
# END ENVY MANAGED CONFIG
```

Darwin 服务的 `mode` 统一使用以下三种语义：

| Mode | Meaning |
|---|---|
| `none` | 明确关闭该服务；Mihomo 同时不生成配置或安装贡献 |
| `manual` | 服务可手动控制，envY 不要求它自动启动或保持运行 |
| `keep` | envY 确保服务启用；Mihomo 由 launchd 自动启动并保持运行 |

OpenSSH 使用 macOS 自带的 sshd，不安装另一套服务。其 `manual` 映射为
nix-darwin 的 `services.openssh.enable = null`，因此保留系统设置中“远程登录”的
当前状态；`none` 和 `keep` 分别映射为 `false` 和 `true`。

Linux managed block 会包含 `envy.linux.*`，不会出现这些 Darwin 服务字段。

## Software Policy

软件仍由所属功能 module 定义，machine 只通过稳定名称表达差异。

公共 Home package：

```nix
{ pkgs, ... }:

{
  envy.software.nix.packages.exclude = [ "okular" ];
  envy.software.nix.packages.include = with pkgs; [ postgresql ];
}
```

Darwin Homebrew：

```nix
{
  envy.darwin.software.homebrew.casks.exclude = [
    "uuremote"
    "microsoft-remote-desktop"
  ];
}
```

单一的 `ENVY MANAGED SOFTWARE` block 直接维护各 ecosystem 的
`include/exclude`。setup 和 `envy sw en/dis` 只修改其中的 exclude 并保留
include；`envy sw add/rm` 先展示 `include +/-`、`exclude +/-` 计划后修改两者。
`rm` 默认增加 exclude，`--clean` 只精简目标 ID 且不改变最终意图。

软件列表状态含义：

| State | Source | Meaning |
|---|---|---|
| `[x]` | `included` | 来自 module include，当前机器会安装。 |
| `[ ]` | `machine exclusion` | 当前 host 的 managed exclusions 主动禁用。 |
| `[-]` | `external exclusion` | managed block 之外的 Nix policy 禁用；setup 不会覆盖。 |
| `[ ]` | `stale exclusion` | exclusion 中仍有名称，但当前 include 已不再提供它。 |

setup 中按 Space 只修改内存里的 pending 状态；按 `s` 退出才进入变更确认与写入。按 `q` 或 Esc 退出不会修改 host 文件。`pending` 表示它与打开 setup 时的 machine exclusion 不同，不表示已经写盘。

`envy config show` 只展示 machine scalar 与 secret 设置状态。软件 policy 使用
`envy sw ls`；`envy sw ls --details` 展示结构化 include/exclude/effective、版本和
canonical reference。Nix reference 是 package contributor 显式声明的来源 metadata：
`nix:<attr>`、`flake:<input>#<attr>` 或 `local:<path>`；不会按最终 package name 猜测。
Linux manifest 直接声明 active groups，不再伪造空的 Homebrew
或 Darwin system/font 列表。

evaluated manifest 使用 `schemaVersion = 3`，软件结构位于
`software.groups.<ecosystem>.<scope>.<kind>`。完整字段见 [software.md](software.md)。

## Mirror Policy

镜像模式是跨平台 machine setting；平台差异由 feature 内部处理：

```nix
{
  envy.mirrors.mode = "china"; # or "upstream"
}
```

公共语言生态使用同一 catalog，Darwin 额外配置 Homebrew，Linux 额外配置 Ubuntu/Debian APT 与 Docker installer。检查最终求值结果使用 `envy mirror status`，连通性检查使用 `envy mirror probe`。完整 ownership 与 bootstrap 边界见 [mirrors.md](mirrors.md)。

新机器可以先通过 [install.md](install.md) 的远程 bootstrap 取得仓库。Bootstrap 只进入现有 setup 流程，不决定 Machine ID、import/copy 或任何软件 policy。

## Feature-first Modules

Home Manager 调用方优先导入功能入口，平台差异由功能内部选择：

```text
modules/desktops/default.nix
  -> desktops/darwin/default.nix
  -> desktops/linux/default.nix

modules/devps/default.nix
  -> common editor/vscode
  -> devps/linux/default.nix (Linux-only host mutation)
```

仓库只有两个组合根

```text
flake.nix
  -> darwin.nix                         (nix-darwin composition)
       -> modules/envy/darwin.nix
       -> modules/agents/darwin.nix
       -> modules/desktops/darwin/nix-darwin.nix

  -> home.nix                           (Home Manager composition)
       -> modules/envy/home.nix
            -> modules/envy/linux.nix   (Linux only)
       -> modules/llm/default.nix
       -> modules/agents/default.nix
       -> modules/cores/default.nix
       -> modules/devps/default.nix
       -> modules/desktops/default.nix
       -> modules/libs/default.nix
```

Python schema 同样拆分为：

```text
resources/scripts/envy/schemas/common/
resources/scripts/envy/schemas/darwin/
resources/scripts/envy/schemas/linux/
```

顶层 dispatcher 组合 common 与当前平台 schema。

## Git Workflow

所有设备使用 `master`：

```bash
envy sync --no-apply
envy check
envy push --self "feat(host): tune work macbook"
```

提交共享模块前使用跨平台检查：

```bash
envy check --all
envy check --changed
envy check --platform linux
envy check --all --build
```

`--changed` 中只有 machine 文件变化时只选这些 targets；出现任意 shared 路径时保守地
选择全部 machines。`--build` 只构建当前平台 target，外平台仍执行 derivation 求值。
CI 在 `aarch64-darwin` 与 `x86_64-linux` 分别运行 Python、shell 和 secret-safety checks。

Push impact 分类：

- machine-only：所有路径都位于 `hosts/darwin/*.nix` 或 `hosts/linux/*.nix`。
- shared：包含任何其他路径，保守地影响全部 `darwin/<id>` 与 `linux/<id>` target。

`--machine-only` 可以包含多个平台的多个 host；`--self` 只允许当前平台与当前 machine 文件。守卫同时检查 worktree 与 outgoing commits，并在 `git add` 前拒绝越界路径。

`envy push` 先检查当前分支、密文和推送范围，再 fetch 目标 remotes 并执行不带 apply 的同步，最后才确认、提交本地改动和推送。同步复用 `envy sync` 的策略：选择彼此线性兼容的最新远端，本地落后时快进，本地与远端分叉时尝试 `git merge --no-edit`；远端之间互相分叉则停止，不自动选择历史。

需要同步且存在未提交改动时，push 会临时 stash 已暂存、未暂存和未跟踪文件，并在同步成功后通过 `git stash apply --index` 恢复原暂存状态；恢复成功才清理本次 stash，已有 stash 不受影响。合并或 stash 恢复发生冲突时立即停止，不继续提交或推送，保留冲突现场与 stash，并显示检查、完成或放弃 merge 以及恢复备份的指引。已有未完成 merge/rebase 等 Git 操作时同样拒绝 push。同步后重新检查实际 worktree 和 outgoing commits 的范围、密文和共享影响，`--yes` 不能跳过这些守卫。整个 push 流程不会 apply，也不会 force push。

Push 在 staging 前验证 worktree、index 与 `HEAD` 都保留加密的
`secrets/secrets.yaml`，并逐个检查所有 outgoing commits 中对应 Git object。任何
现存版本不是 sops 密文，或当前三层缺失该文件时都会拒绝 push；这项
检查独立于 `--self`、`--machine-only` 和 `--yes`，不能被确认选项跳过。多 remote
push 会分别报告 succeeded、failed、unchanged；部分失败时保留已成功结果并输出每个
失败 remote 的精确重试命令。

默认 branch 是 `master`。`--branch` 只用于明确操作当前临时分支，不参与 host 或平台选择。
