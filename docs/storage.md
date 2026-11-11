# 存储生命周期规则与验证

本页记录 `envy.storage` 的规则能力、当前机器策略、基线、只读验证结果与 NAS 落地步骤。当前机器为 `report` 模式：`envy storage plan` 只生成候选，不移动、不删除、不归档任何文件。

## 改动文件

| 文件 | 改动 |
|---|---|
| `hosts/darwin/macbook_air.nix` | 在 `ENVY MANAGED CONFIG` 内加入 13 条规则、7 条 `protectedOverrides` 与 `maintenance`，`mode = "report"` |
| `modules/storage/options.nix` | 新增 `targets`、`depth`、`names`、`recursiveAge`、`processes`、`protectedOverrides`、`maintenance` 及约束断言 |
| `modules/storage/default.nix` | 定时器仅在 `mode = "auto"` 时注册 |
| `resources/scripts/envy/storage/model.py` | 目录规则字段、整树哈希、条目校验、受保护路径的精确豁免 |
| `resources/scripts/envy/storage/planner.py` | 通配符展开、按深度生成 organize 配置、进程阻塞、0 候选诊断、`validate` |
| `resources/scripts/envy/storage/lifecycle.py` | 目录整树 rename/复核、`rmtree` 释放、`copytree` 恢复 |
| `resources/scripts/envy/storage/maintenance.py` | 新增：Nix 无引用路径（保留 generations）、Docker、Go 的显式维护动作 |
| `resources/scripts/envy/storage/cli.py` | `maintain`、`validate` 命令与 plan 逐规则汇总 |
| `resources/scripts/envy/config/machine_nix.py` | 重写 `ENVY MANAGED CONFIG` 时保留块内手写策略 |
| `resources/scripts/envy/keys/prompts.py` | USB 密钥扫描跳过不可读挂载点（如无权限的 SMB 卷） |
| `resources/scripts/tests/test_storage.py` | 目录生命周期、进程阻塞、维护与校验测试 |
| `resources/scripts/tests/test_machine_config.py` | 验证手写规则在 `envy config` 重写后仍保留 |
| `resources/scripts/tests/test_key_import.py` | 新增不可读卷扫描测试 |
| `.gitignore` | 忽略 `.nas_cred` |
| `docs/storage.md` | 本交付文档 |

新文件 `resources/scripts/envy/storage/maintenance.py` 与本文档已 staged（未提交），以便标准 flake 源纳入文件；提交仍由用户决定。

## 基线（阶段 1）

2026-11-10 使用 `ncdu -0 -rr -x -e -t 2` 只读扫描 `/Users/chi`、`/System/Volumes/Data`（排除 home）与 `/nix`：

- APFS 容器约 245 GB，已用约 186 GB，可用约 59 GB。
- home 分配 65.18 GiB（516,711 个文件）；OrbStack 稀疏磁盘名义 228 GiB、实际约 4.69 GiB（ncdu apparent 被稀疏文件放大）。
- `Library` 48.7 GiB：Application Support 23.2、Containers 10.3、Caches 8.0、Group Containers 5.1。
- `.vscode` 3.8 GiB、`.codex` 2.7 GiB；Data 卷 39.4 GiB（Applications 18.8）；Nix 约 29.7–32.5 GiB。
- Nix 基线无引用路径 12,869 条，估算约 17.3 GiB（后续预览已随 store 变化）。
- 重点目标：Codex Sparkle 2.633 GiB、飞书 update 2.453、飞书 Service Worker CacheStorage 5.218（7 个根）、go-build 1.459、VS Code ShipIt 0.94、微信 cache 文件 0.564、微信日志 0.569、VS Code VSIX 0.362、旧安装包 0.016。

organize 3.3.0 实测能力与差距：

- `Location` 支持 `path`、`min_depth`、`max_depth`、`search`、`exclude_files`、`exclude_dirs`、`filter`；walker 一律跳过符号链接。
- `locations.path` 不展开通配符（`*` 得 0 匹配），由 envY 先展开为显式根目录。
- `lastmodified` 只看目录条目自身的 mtime，无法表达“整棵子树都过期”；因此使用 `recursiveAge` 生成 Python 过滤器。
- organize 仅负责选择；`sim` 的 `echo` 只标记候选，实际移动/删除由 envY 重新校验后执行。
- `organize check` 可独立验证生成的配置，现有 `envy storage validate` 即基于该命令。

## 规则能力

| 字段 | 作用 |
|---|---|
| `path` | 绝对路径，可用 `*`；通配符由 envY 展开 |
| `targets` | `files` 或 `dirs`；目录规则按整棵树处理 |
| `depth` | 根目录以下的扫描深度；`1` 表示直接子项 |
| `names` | 精确名称过滤 |
| `recursiveAge` | 要求目录及全部子项都超过 `minAgeDays` |
| `processes` | 必须退出的进程；含 `/` 的模式匹配完整命令行 |
| `extensions` / `minAgeDays` / `minSizeBytes` | 文件过滤条件 |
| `action` | `quarantine` 或 `archive`；目录仅支持隔离，归档只接受普通文件 |

`protectedOverrides` 只能指向受保护应用数据根之下的具体缓存或更新目录；`checkout`、`state`、`.ssh`、`.gnupg`、`sops`、Keychain、Mail、Messages 永远不可豁免。

## 当前机器规则（report）

| 规则 | 候选 | GiB | 说明 |
|---|---:|---:|---|
| feishu-web-cache-aha | 2 | 0.766 | 飞书运行中，blocked |
| feishu-web-cache-iron | 3 | 0.699 | 飞书运行中，blocked |
| feishu-update | 0 | 0 | 最新条目 3.5 天，低于 14 天 |
| feishu-logs | 0 | 0 | 最新匹配条目 0.0 天，低于 30 天 |
| codex-sparkle | 0 | 0 | 最新条目 0.1 天，低于 14 天 |
| vscode-shipit | 0 | 0 | 最新条目 3.2 天，低于 14 天 |
| go-build-cache | 0 | 0 | 最新条目 4.2 天，低于 30 天 |
| wechat-cache-dirs | 1 | 0.000 | 微信运行中，blocked |
| wechat-cache-files | 82 | 0.146 | 仅 ≥1 MiB 文件；blocked |
| wechat-web-cache | 0 | 0 | 最新条目 0.2 天，低于 14 天 |
| wechat-web-cachestorage | 0 | 0 | 最新条目 4.4 天，低于 14 天 |
| wechat-logs | 2 | 0.003 | 可执行候选 |
| old-installers | 3 | 0.016 | 可执行候选 |

合计 93 个候选、1.63 GiB；88 个因应用运行被标记 blocked，5 个可执行。门限未到的规则不会因退出应用而出现候选。

## 凭据与安全边界

- 仓库根 `.nas_cred` 权限 `0600`，已加入 `.gitignore`（`.gitignore:17`），`git ls-files` 确认未跟踪。
- 密码未写入 Nix、日志、测试、plan 记录或提交内容；NAS 访问仅使用 SSH 密钥。
- 归档加密口令仍只经 sops 的 `storage/archive/password` 引用，尚未创建该密钥。

## NAS 验证结论

- `nas.fl0wer.cn` 解析到私网地址，SSH 22 端口可达；`xikai` 可用现有 SSH 密钥非交互登录。
- restic 的 SFTP 后端通过 `ssh` 建连，**不接受仓库密码作为 SSH 密码**：认证依赖密钥或 ssh-agent，`~/.nas_cred` 的密码不能直接传给 restic，本机也没有 `sshpass`。
- 只读探针 `restic -r sftp:xikai@nas.fl0wer.cn:/home/xikai/envY-archive snapshots` 返回“仓库不存在”，说明传输与认证可用，且未创建远端内容。
- SFTP 家目录在 63G 系统盘（剩余约 50G）；`/vol1` 对该用户不可读。
- SMB 共享已挂载在 `/Volumes/share`，剩余约 907G。

后续落地步骤（尚未执行）：

1. 大容量优先：`/Volumes/share/envY/archive`，需先配置 SMB 开机自动挂载。
2. 或 `sftp:xikai@nas.fl0wer.cn:/home/xikai/envY-archive`（受 50G 限制）。
3. 在机器文件中设置 `envy.storage.archive.repository`，在 sops 中创建 `storage/archive/password`。
4. 首次执行 `envy storage init` 写入仓库；archive 规则启用前先验证一次恢复。
5. 注意：`nas` SSH alias 是 `chi@...:6611`，其 SFTP subsystem 不可用；归档不要使用该 alias。

## 原生维护

`envy storage maintain` 默认只预览；执行需 `--yes`，且不进入定时任务：

- Nix：`nix-collect-garbage` 保留 generations（不使用 `-d`）；Darwin 经 `esudo`。预览时报告 10,777 条无引用路径（2026-11-10 实测，计数随 store 变化）。
- Docker：`docker builder prune` 与悬空镜像清理，仅通过 Docker 原生命令。
- Go：`go clean -cache`。

## 验证命令与结果

| 命令 | 结果 |
|---|---|
| `envy storage validate --json` | 24 个生成配置全部通过 `organize check` |
| `envy storage plan --json` | 93 候选 / 1.63 GiB；每条规则均有候选或 0 候选原因 |
| `envy storage maintain --json` | 预览成功；Nix 报告 10,777 条无引用路径，未执行 prune |
| `envy config check` | 设备、机器、代理、软件、密钥路径均通过 |
| `envy host check macbook_air` | 机器配置求值通过 |
| `nix flake check path:. --no-build` | 通过（跳过 x86_64-linux） |
| `nix build .#checks.aarch64-darwin.envy-lint` | 通过 |
| `nix build .#checks.aarch64-darwin.envy-storage-tests` | 34 项通过 |
| `nix build .#checks.aarch64-darwin.envy-python-tests` | 844 项通过（skipped=15） |
| `nix run .#envy -- storage --help` | 命令注册正常 |
| machine config 单测 | 14 项通过 |

## 可复现性与状态

- 新文件已 staged（未提交）；标准 `.#checks` 与 `nix run .#envy` 均可复现。
- `~/.local/state/envy/storage` 只有 plans，没有 runs/quarantine。
- 未执行 `storage run/purge/init`、`apply`、`darwin-rebuild switch`。
- Linux 分支只做求值，未构建或运行。
- 应用运行期间 blocked 的候选需退出应用后重新生成计划。
