# envY

跨平台 Nix 配置，支持 Darwin（aarch64）与 Linux（x86_64），由 nix-darwin、Home Manager 和 sops-nix 组成。

## 结构

- flake.nix：扫描 hosts/darwin/*.nix 与 hosts/linux/*.nix，生成机器 target。
- hosts/<platform>/<id>.nix：该机器全部非敏感配置的唯一来源。
- .device-label：Git 忽略的本机选择信息，只包含 machine ID 和 sops key label。
- modules/envy：选项、软件聚合和 machine manifest。
- modules/mirrors：镜像 catalog 与平台实现。
- modules/software：软件来源、搜索、验证和安装后端。
- modules/agents：agent、provider wrapper 和 skill 分发。
- secrets/secrets.yaml：sops 加密文件；明文只在激活阶段出现。

机器差异写在 host module 中，跨平台选项使用 envy.*，仅平台专属选项使用 envy.darwin.* 或 envy.linux.*
## 安装
```shell
bash install.sh
# 或
bash setup.sh
```
需要固定 release、选择镜像或复用已有 checkout 时，参见 docs/install.md。初始化机器：
```shell
envy host init <machine-id>
envy host select <machine-id>
envy config check
envy apply
```
## 常用命令

| 命令 | 用途 |
|---|---|
| envy status | 查看机器、Git、generation、软件和 doctor 状态 |
| envy check [--all|--changed|--build] | 求值或构建 target |
| envy plan | 构建并比较 active generation |
| envy sync --no-apply | 同步共享 master，不应用配置 |
| envy update inputs [name] | 更新 input，验证失败则回滚 lock |
| envy software / envy sw | 查看 manifest 与 include/exclude 状态 |
| envy sw add/rm/en/dis/search | 管理软件并验证 canonical reference |
| envy sw source ls/show/search/add/rm/sync | 管理软件来源 |
| envy mirror status/probe | 查看或只读探测镜像 |
| envy mirror set/reset <target> | 修改机器级生态镜像覆盖 |
| envy doctor | 检查配置、应用、状态、登录和权限 |
| envy key status/list/repair | 检查和修复 age/sops 密钥 |
| envy tui | 启动通过 JSON 调用 CLI 的 Ratatui 前端 |

## 镜像与来源

默认 profile 是 envy.mirrors.mode = "china"，也可选 upstream。bootstrap 只提供首次运行所需的临时环境；envy apply 后由 modules/mirrors 维护持久配置。两阶段必须使用同一 catalog。

- binary cache、npm、PyPI/uv、Go、Rust/Cargo、Homebrew、APT 等按 docs/mirrors.md 的 ownership 处理。
- Flake source mirror 只替换 lock 中有固定 narHash 的 GitHub/GitHub API source，失败时保留原 URL 回退，不改写 flake.nix 或 flake.lock。
- 不自动运行 chsrc、不覆盖系统拥有的 APT source、不通过不可信代理重写固定 hash URL。
- 软件搜索、添加和安装共用 source identity；不能验证 canonical reference 时不改 machine policy。

## Secret

```shell
sops --decrypt secrets/secrets.yaml
sops updatekeys secrets/secrets.yaml
```
完整密钥轮换、恢复和事务行为见 docs/key.md。

## 维护入口

- 机器：docs/machines.md
- 安装：docs/install.md
- 镜像：docs/mirrors.md
- 软件与来源：docs/software.md
- agent 与 skill：docs/agents.md 与 docs/agent-skills.md
- doctor：docs/doctor.md
- TUI：docs/tui.md
