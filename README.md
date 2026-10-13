# envY — Cross-platform Nix Configuration

基于 Nix Flakes、nix-darwin、Home Manager 与 sops-nix 的声明式 Darwin/Linux 配置

## Architecture

```text
flake.nix
├── darwin.nix                       # 唯一 nix-darwin 组合入口
├── home.nix                         # 唯一 Home Manager 组合入口
├── hosts/default.nix
├── hosts/darwin/<machine-id>.nix
├── hosts/linux/<machine-id>.nix
├── modules/envy/                  # option、聚合和 machine manifest
├── modules/mirrors/               # 公共 catalog 与 Darwin/Linux 镜像实现
├── modules/llm/                   # 公共 LLM secrets、环境模板与 shell 接入
├── modules/cores/                 # 公共 shell、Git、SSH 和工具
├── modules/desktops/
│   ├── default.nix                # 公共功能入口
│   ├── darwin/                    # Darwin 专有实现
│   └── linux/                     # Linux 专有实现
├── modules/devps/                 # 公共编辑器；Linux 实现在 linux/
└── modules/agents/                # 公共 agent 功能及 Darwin/Linux 分发实现
```

`flake.nix` 分别扫描两个 host 目录：

- `hosts/darwin/*.nix` 生成 `darwinConfigurations.<machine-id>`。
- `hosts/linux/*.nix` 生成 `homeConfigurations.<machine-id>`。

检测到旧 `~/.config/dotfiles/config.nix` 或 `/etc/dotfiles/config.nix` 时，flake
还会按其中的身份与 Linux policy 生成临时 `homeConfigurations.default`，仅用于
旧 `master` 第一次同步时兼容原来的 `.#default`。没有旧配置时不会猜测某个
versioned host 作为默认机器。

flake 不再分别拼装散落的系统模块。Darwin 配置只导入 `darwin.nix`，Linux
和 Darwin 的用户配置都由 `home.nix` 组合；具体平台实现仍归各功能目录所有。
`home.nix` 本身只保留 Home Manager 身份初始化，secret、template 和平台环境均由消费它们的 feature 声明。

每台机器的 host module 是全部非敏感机器配置的唯一来源。Git 忽略的 `.device-label` 只保存本机选择的 `machine_id` 与 sops key label，不参与 Nix policy。

Option 遵循公共优先原则：

- 多平台语义和处理方式一致时使用 `envy.user.*`、`envy.git.*`、`envy.llm.*`、`envy.vscode.mode`、`envy.software.nix.packages.*`
- 只有平台专有能力使用 `envy.darwin.*` 或 `envy.linux.*`

## Quick Start

一键取得仓库并进入交互式 setup：

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://gh-proxy.com/https://raw.githubusercontent.com/Kie-Chi/envY/master/install.sh | bash
```

国内 bootstrap 默认通过 `gh-proxy.com` 获取 GitHub 仓库，并在无法访问代理时回退到
GitHub 直连；Nix binary cache、npm、PyPI、Go 和 Rust/Cargo 的首次 setup 下载也会使用国内镜像。
如果网络可以直连 GitHub，可在命令后追加 `--mirror upstream`。

或者手动 clone：

```bash
git clone https://github.com/Kie-Chi/envY.git ~/.envy
cd ~/.envy
bash setup.sh
```

远程命令会执行当前 `master` 上的脚本。需要先审查内容、固定 release tag、自定义 clone 目标或只下载仓库时，请看 [docs/install.md](docs/install.md)。

当当前 Machine ID 尚无 host 文件时，可以选择：

- `import`：创建小型 host module 并导入 `hosts/default.nix`，持续继承公共默认值。
- `copy`：复制当前默认 policy，形成不随默认值变化的独立快照。

初始化只创建文件，不替用户决定安装或排除哪些软件：

```bash
envy host init <machine-id>
envy host list
envy host status
envy host check <machine-id>
envy host select <machine-id>
```

## Daily Workflow

| Command | Purpose |
|---|---|
| `envy status` | 用一个一致快照展示当前 machine、Git、generation、software、Doctor 摘要与推荐下一步；`--json` 供 TUI 使用 |
| `envy apply` | Refine 并应用当前平台的 machine target |
| `envy sync` | 从远端快进共享 `master`，成功后应用当前机器 |
| `envy sync --no-apply` | 仅同步，不应用 |
| `envy sync --build-only` | 同步后只构建当前平台 target |
| `envy push "<message>"` | 分析 worktree 与 outgoing commits，确认影响范围后提交并 push |
| `envy push --machine-only` | 只允许 `hosts/darwin/*.nix` 与 `hosts/linux/*.nix` |
| `envy push --self` | 只允许当前平台、当前 `.device-label` 所选 host 文件 |
| `envy check` | 求值当前 machine target |
| `envy check --all` | 求值仓库中全部 Darwin/Linux machine targets |
| `envy check --changed` | 根据 worktree 路径检查受影响的 machine targets |
| `envy check --build` | 在本机平台构建所选 targets；外平台仍只求值 |
| `envy plan` | 构建但不激活当前 target，并与 active generation 比较 closure |
| `envy history` / `envy history diff <a> <b>` | 查看 generations 或比较两个 generation 的 closure |
| `envy tui` | 启动 Rust/Ratatui 全屏 frontend；通过 JSON 调用现有 envY backend |
| `envy rollback <n> --dry-run` | 在激活旧 generation 前预览 closure 差异 |
| `envy mirror measure <target> --provider chsrc|curl` | 使用指定测速 provider 测量候选镜像；默认 `chsrc` |
| `envy update` | 更新全部 flake inputs、检查全部 machines，并在 Darwin 更新 Homebrew metadata |
| `envy update inputs [name]` | 更新全部或指定 flake input；验证失败自动恢复 `flake.lock` |
| `envy clean --older-than 30d` | 经确认后只清理指定期限以前的 generations |
| `envy config check` | 只读检查 device metadata、host module 与 secrets |
| `envy config refine` | 迁移并补全本平台 machine/schema |
| `envy config show` | 展示求值后的 machine scalar 与 secret 设置状态 |
| `envy host diff <a> <b>` / `envy host matrix` | 比较两台 machine，或展示所有 machine 的 effective software 覆盖 |
| `envy software` / `envy sw` | 展示当前机器的 evaluated software policy |
| `envy sw ls --details` | 展示软件版本、引用以及 include/exclude/effective 状态 |
| `envy sw add/rm <item>` | 自动判断或交互选择软件类型，预览 include/exclude 计划后改变 desired state；也兼容原有 `<group> <item>` 精确形式 |
| `envy sw en/dis <group> <id>` | 启用或排除一个稳定 software ID |
| `envy sw search <query>` / `envy sw se <query>` | 并发搜索全部当前可用的软件 registry；重复查询优先复用缓存 |
| `envy sw status` / `envy sw st` | 汇总当前机器的软件 group 和 selection 状态 |
| `envy sw audit` / `envy sw why <item>` | 检查冗余/歧义 policy，或解释软件为何生效/被排除 |
| `envy sw cache status` | 查看供 search/add 共用的精确 registry identity index |
| `envy mirror status` | 展示当前 machine 求值后生效的镜像端点 |
| `envy mirror probe` | 只读探测镜像 HTTP 状态与延迟 |
| `envy mirror targets` / `envy mirror sources <target>` | 按 npm/rust/python/go target 列出可选镜像源；结果来自 envY cache、chsrc 或 catalog fallback |
| `envy mirror measure <target> [--provider chsrc|curl]` | 使用 provider 专属 cache 运行/读取测速；curl 测量生态代表性资源，`--refresh` 强制重测 |
| `envy mirror set <target> <source>` / `reset <target>` | dry-run 后把选择写入当前 machine 的 generated override block；不调用 `chsrc set` |
| `envy mirror cache status/clean` | 查看或清理候选源与测速 cache |
| `envy habit list/show/set/check/repair` | 管理跨平台个人交互习惯的 machine policy，并检查或重新应用对应实现 |
| `envy doctor` | 检查本平台配置、应用、状态与登录信息；TCC 仅在 Darwin 加载 |
| `envy doctor system` | 检查运行依赖、apply runner、Git 状态与中断残留 |
| `envy doctor network` | 只读探测求值后的镜像端点 |
| `envy key repair` | 修复中断的 key rotation 标记、权限并重新验证密文 |

新增 registry 软件时，`sw add` 不会仅凭输入合成 reference：它必须命中
evaluated manifest、精确 identity index，或完成一次对应 provider 的精确查询。
`--offline` 只允许使用已有 positive cache；`--refresh` 强制重新解析。
补全同样理解这些结构：`sw add` 会读取所选 group 对应的 fresh identity index，
`sw why` 会补全跨 group 的 evaluated/受管条目，`history diff` 与 `rollback`
会补全真实 generation，`host diff` 与 `host matrix --group` 则补全跨平台 machine
和 canonical group。按 Tab 只做本地只读查询，不会访问 registry 或求值所有 machines。
`agent set` 与 `config set` 都会补全当前机器的 agent、endpoint 和 credential
字段，并按字段提示布尔值、API/协议、adapter 及已声明的连接引用。
`agent show/rm`、`agent add` 的连接选项和 `credential-set` 的逻辑名称/YAML
路径也支持补全；候选不读取或展示密钥值，模型名与 URL 仍可自由输入。
`sw source show/add/rm/sync` 和 `sw add/search --source-id` 支持来源 ID，
软件缓存候选按当前来源与上下文指纹过滤；`remote-agent start` 则从所选 TOML
补全目标和 `--agent` 名称，不建立远程连接。
`config show`、`host list/status`、`software list/status/audit/why`、
`mirror status/probe/targets/sources/measure` 与 `doctor` 的只读视图支持 `--json`。
`sw add/rm --json` 则输出稳定的 `software.<action>` envelope，包含完整计划和
应用结果；JSON mutation 必须显式传入 `--yes`，方便未来 TUI 分两步渲染预览与确认。
TUI 边界和两阶段 mutation 协议见 [docs/tui.md](docs/tui.md)。

所有耗时的前台外部命令会在安静超过数秒后显示任务名称、已耗时和 Ctrl-C
取消提示。`envy --debug <command>` 进一步输出安全遮罩后的命令起止、耗时、
退出码、manifest cache 命中情况和 Nix 求值阶段；diagnostic 写入 stderr，
不会破坏 `--json` stdout。

`envy push` 和 `envy sync` 默认要求当前分支为 `master`。`--branch` 只是显式操作临时分支。
共享变更 push 前建议运行 `envy check --all`；push 会同时检查 worktree、index 和 outgoing commits，拒绝任何明文 `secrets/secrets.yaml`。

国内网络默认使用 `envy.mirrors.mode = "china"`。首次 setup 的临时环境、APT/Homebrew 行为、探针及不能透明镜像的下载见 [docs/mirrors.md](docs/mirrors.md)。

## Machine Policy

公共 Home Manager 软件在两边使用同一 option：

```nix
{ pkgs, ... }:

{
  imports = [ ../default.nix ];

  envy.vscode.mode = "remote";

  envy.software.nix.packages.exclude = [
    "okular"
  ];

  envy.software.nix.packages.include = with pkgs; [
    postgresql
  ];
}
```

Darwin 专有 policy 只出现在 Darwin host：

```nix
{
  envy.darwin.services.mihomo.mode = "none";
  envy.darwin.services.mihomo.tun = false;
  envy.darwin.services.openssh.mode = "manual";
  envy.darwin.software.homebrew.casks.exclude = [ "uuremote" ];
}
```

Darwin 服务统一使用 `none`、`manual`、`keep`：分别表示关闭、不要求常驻且可手动控制、以及由 envY 确保启用。OpenSSH 的 `manual` 保留 macOS“远程登录”的当前状态。

Linux 专有 policy 只出现在 Linux host：

```nix
{
  envy.linux.desktop = "gnome";
  envy.linux.option = "desktop";
}
```

`envy.linux.option = "server"` 会禁用整个 Linux desktop 层，忽略 desktop
selector。`option = "desktop"` 时，`envy.linux.desktop` 分别使用 `gnome`、
`niri`、`all`（两者）或 `none`

软件 group 统一使用 `<ecosystem>.<scope>.<kind>`，例如
`nix.user.package`、`homebrew.system.cask`、`native.system.package`、
`npm.user.tool` 和 `pypi.user.tool`。完整 schema、搜索来源和生命周期语义见
[docs/software.md](docs/software.md)。

## Secrets

- 非敏感值：`hosts/<platform>/<id>.nix` 中的 `envy.*` options。
- 敏感值：sops 加密的 `secrets/secrets.yaml`。
- Darwin age key：`~/Library/Application Support/sops/age/keys.txt`。
- Linux age key：`~/.config/sops/age/keys.txt`。

!!!不要把 API key、密码或带 token 的 URL 放进 Nix 求值期表达式
新增设备的 age public key 加入 `.sops.yaml` 后运行：

```bash
sops updatekeys secrets/secrets.yaml
```

- 完整安装说明见 [docs/install.md](docs/install.md)
- machine 设计见 [docs/machines.md](docs/machines.md)
- 跨平台交互习惯契约见 [docs/habits.md](docs/habits.md)
- age/sops 密钥生命周期见 [docs/key.md](docs/key.md)
- doctor 维护说明见 [docs/doctor.md](docs/doctor.md)

## License

[MIT License](LICENSE) © 2026 Kie-Chi
