# 存储生命周期规则与验证

更新日期：2026-11-12 UTC。envY 提供共享默认规则，机器按稳定 ID 增加、排除和局部覆盖。最新变更补齐微信和 Zotero 的更新缓存规则，收紧飞书和 VS Code 的暂存选择范围，并补充后台更新器拦截。下文保留 2026-11-11 的 NAS 合成归档验证与只读报告，历史统计按各自记录时间解释。

## 共享规则与机器选择

规则由 `modules/storage/rules/common.nix`、`darwin.nix`、`linux.nix` 提供，`default.nix` 组合当前平台贡献。路径从 `config.envy.user.home` 推导，机器无需导入 `hosts/default.nix` 才能获得默认规则。

`envy.storage.rules.include.<id>` 是合并后的定义，`exclude` 优先，`effective` 是只读最终列表。Nix 负责合并，Python 只消费有效结果。共享字段使用 `mkDefault`，同优先级的冲突定义报错，机器只需指定发生差异的字段：

```nix
# Keep the shared rule but change one field.
envy.storage.rules.include.old-installers.minAgeDays = 60;

# Remove a shared rule, including its rule-owned protection grants.
envy.storage.rules.exclude = [ "chrome-code-cache" ];

# Add a machine rule; missing custom roots fail unless explicitly optional.
envy.storage.rules.include.local-build-cache = {
  path = "${config.envy.user.home}/.cache/local-build";
  action = "quarantine";
  minAgeDays = 45;
};
```

规则携带 `id`、`source`（`default:common`、`default:darwin`、`default:linux` 或 `machine`）、`overriddenFields`、`optionalRoot` 与 `protectedOverrides`。status、validate 和 plan 保留来源、覆盖、排除、缺失与阻塞原因。共享规则显式设置 `optionalRoot = true`；机器自定义规则默认 false。目录不存在可以跳过，权限错误、非法路径与配置错误不能被当作“未安装应用”。

旧 Nix `rules = [ ... ];` 已迁移且会被类型检查拒绝。旧版本 manifest 的规则列表仍可读取；模型版本为 2，指纹含模型版本、规则元数据与最终政策，旧 plan 不可执行。config/setup 重写 managed block 时保留机器级 include、exclude、局部覆盖与自定义规则。

当前选择：Air 为 `report`，29 条 Darwin 默认规则中因 native Go 选择排除 `go-build-cache`，得到 28 条有效规则；另一台 Darwin 保持 `manual`，获得 29 条默认规则；Linux 保持 `manual`，获得旧安装包与 Go 年龄规则两项。没有启用 auto 或定时器。`report` 阻止默认及定时执行；显式 `run --yes` 仍是写入操作。

## 更新下载包规则

更新下载和解包暂存通过共享规则进入 `plan → run → quarantine → purge` 生命周期。路径均从机器 home 推导；下表路径相对 `~/Library/`。

| 规则 ID | 选择范围 |
|---|---|
| `feishu-update` | `Application Support/LarkShell/update` 下的 `update.noindex`、`update_downloading` 两个目录；授权也仅限这两个子目录 |
| `codex-sparkle` | `Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation` 的直接子目录 |
| `vscode-shipit` | `Caches/com.microsoft.VSCode.ShipIt/update.*` 下名为 `Visual Studio Code.app` 的目录 |
| `wechat-sparkle` | `Caches/com.tencent.xinWeChat/org.sparkle-project.Sparkle/Installation` 本层和直接会话目录内的 DMG；不进入解包应用 |
| `zotero-update-archive` | `Caches/Zotero/updates/Applications/Zotero/updates/0/update.mar` 普通文件 |
| `zotero-update-staging` | 同一 Zotero 更新根下的 `Updated.app` 目录 |

这六条规则均采用 14 天年龄门限、可缺失根和 quarantine。微信原始包采用 `depth = 2` 和 `extensions = [ "dmg" ]`；其余为 `depth = 1`，目录还要求所有后代均满足年龄。Zotero 路径对应 `/Applications/Zotero.app` 的实际缓存布局，其他布局可通过机器的同 ID `path` 覆盖。更新器程序、状态文件、Sparkle 的 `Launcher` 和已安装应用不在选择范围内。缺失缓存会在报告中显示跳过原因。

应用主进程和相关后台更新器都会阻止操作：完整命令行模式覆盖应用 bundle、Sparkle Launcher、ShipIt、飞书 darwin-updater 和 Zotero 更新路径。即使主应用已退出，后台更新器仍活跃也会被跳过；执行已保存计划前还会重新检查。storage 不退出、重开或升级应用。

解包应用若包含框架或资源符号链接，仍会被既有目录保护跳过；本次没有放宽符号链接和硬链接限制，目录占用不能直接算作可回收量。

这套规则按年龄和活动状态判断过期缓存，尚未实现“确认安装成功后立即删除”的版本或完成状态条件。近期下载即使无人占用，也不会因为目录较大而入选。默认 quarantine 保留 30 天，移动到隔离区不立即释放磁盘空间；到期 purge 才释放本地副本。`report` 只预览，启用自动执行需明确选择 `auto`。

专用 storage 检查把实际 Nix 目录导出为 fixture，在隔离 home 中用真实 organize 验证选择范围、近期后代、后台更新器阻塞、执行前复查、可缺失根和隔离恢复；fixture 不读取真实应用数据。

## 能力与安全边界

| 字段 | 语义 |
|---|---|
| `path` | home 下的绝对路径，可有内部 glob；envY 自行展开并保留权限错误 |
| `targets` / `depth` / `names` | 文件或目录、扫描深度、精确名称；depth=1 为直接子项 |
| `recursiveAge` | 目录及子树所有条目均需超过年龄门限 |
| `processes` | 必须退出的进程；带斜杠的模式匹配完整命令行 |
| `extensions` / `minAgeDays` / `minSizeBytes` | 文件后缀、年龄和大小条件 |
| `action` | quarantine 或 archive；目录仅支持 quarantine，archive 只支持普通文件 |
| `protectedOverrides` | 该有效规则所需的精确缓存/更新目录授权；排除规则会撤销其授权 |

不放开整个 Application Support、Containers 或用户 profile。state、checkout、SSH/GPG/sops 密钥、Keychain、Mail、Messages 及显式保护路径不可豁免；候选包含这些路径的祖先目录也受保护。内部通配符可以定位用户缓存，但授权末段必须是明确目录。符号链接、硬链接、不安全所有者、权限未知及活动文件检查失败均拒绝操作。

相同路径及父子候选重叠会报告涉及的规则并拒绝整个 plan；执行存储 plan 前再次检查。文件执行前复核签名与内容，归档后读回比较 SHA-256；目录用子树相对路径、类型、大小与 mtime 的摘要复核结构和元数据，该摘要不等于逐文件内容哈希。隔离通过 rename 延迟释放，默认保留 30 天；rename 本身不立即释放空间。恢复与过期释放也需重新验证，归档不会自动 restic forget。

organize-tool 使用本地打包的 3.3.0，负责 check/sim/选择；envY 负责重验与实际修改，restic 负责归档和恢复。实测 organize 的 locations.path 不展开通配符，目录 lastmodified 只检查目录自身 mtime；envY 分别补充路径展开和 recursiveAge。walker 跳过符号链接，无法据此绕过 envY 的安全校验。

## 迁移基线与 2026-11-11 只读报告

2026-11-11 13:54 UTC 的私有基线记录 Air/report/13 规则、Pro/manual/0 规则、Linux/manual/0 规则，92 个候选、1,699,840,031 字节、87 blocked。纯迁移时逐项比较原 13 条规则的所有原始字段，差异为零；另存 migration.json 后才加入新覆盖和 Go 策略排除。

较早的 2026-11-10 ncdu 调查：APFS 约 245 GB，总已用约 186 GB；home 分配约 65.18 GiB；Library 48.7 GiB，Caches 8.0 GiB；Nix 约 29.7–32.5 GiB。OrbStack 磁盘名义约 228 GiB、实际分配约 4.69 GiB。这些是历史占用背景，不是当前可回收量；无引用 store 路径计数也会随构建变化。

本轮起点的标准 CLI 只读报告有 19 条规则、92 个候选、1,699,840,031 字节、87 blocked。最新报告时间：2026-11-11T17:20:21.812032+00:00。标准 `nix run .#envy -- storage status/validate/plan/maintain --json` 确认 25 条有效规则；validate 检查 34 个根配置，缺失的 optional 根单列跳过；plan 共 92 个候选、878,178,335 字节（约 0.818 GiB），87 个 blocked。未被进程条件阻止的 5 项共 20,811,311 字节（约 19.85 MiB），执行仍需活动文件与完整性复核。

| 有效规则 | 候选 | 字节 | blocked | 原因或状态 |
|---|---:|---:|---:|---|
| chrome-cache | 0 | 0 | 0 | 可选路径未匹配根目录 |
| chrome-code-cache | 0 | 0 | 0 | 可选路径未匹配根目录 |
| chrome-shader-cache | 2 | 1,122,304 | 2 | 对应应用仍运行 |
| codex-shader-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| codex-sparkle | 0 | 0 | 0 | 对应应用仍运行；可选根目录不存在 |
| feishu-iron-shader-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 1.1 天，未到 30 天 |
| feishu-logs | 0 | 0 | 0 | 最新条目约 0.0 天，未到 30 天 |
| feishu-shader-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| feishu-update | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.3 天，未到 14 天 |
| feishu-web-cache-aha | 0 | 0 | 0 | 对应应用仍运行；无匹配名称/扩展名 |
| feishu-web-cache-iron | 2 | 694,431,744 | 2 | 对应应用仍运行 |
| old-installers | 3 | 17,093,951 | 0 | 通过进程条件，执行前仍需复核 |
| steam-shader-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| vscode-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| vscode-cached-data | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.1 天，未到 30 天 |
| vscode-gpu-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| vscode-logs | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| vscode-shipit | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 14 天 |
| vscode-vsix | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| vscode-vsix-trash | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.0 天，未到 30 天 |
| wechat-cache-dirs | 1 | 0 | 1 | 对应应用仍运行 |
| wechat-cache-files | 82 | 161,812,976 | 82 | 对应应用仍运行 |
| wechat-logs | 2 | 3,717,360 | 0 | 通过进程条件，执行前仍需复核 |
| wechat-web-cache | 0 | 0 | 0 | 对应应用仍运行；最新条目约 0.1 天，未到 14 天 |
| wechat-web-cachestorage | 0 | 0 | 0 | 对应应用仍运行；最新条目约 6.3 天，未到 14 天 |

本轮与起点之间，原 19 条规则的全部定义字段逐项一致。两条旧飞书 AHA 候选目录在最终复查时已不存在，移出报告 822,784,000 字节；新 `chrome-shader-cache` 增加两个候选、1,122,304 字节，均被 Chrome 进程阻止。其它保留候选的大小没有变化。总字节因此变为 878,178,335；不能将扫描期间的目录消失或空闲空间变化归因于新规则。本轮没有执行这些用户候选。

候选文件统计用 st_size，目录用子树 st_blocks × 512，混合汇总不代表立即可释放空间。逐条完整路径、类型、depth、names、年龄、进程与原因见本轮私有 `rule-report-final.csv/json`；与起点的路径级差异保存在 `report-comparison.json`。

## 本轮磁盘调查与六条新增规则

只读提权通过既有私密凭据通道完成，密码经 stdin 认证，没有写入命令参数、日志、文档或 goal，也没有新增或替换 sops 凭据。根层 du 的 Data 已统计约 102.87 GiB、Users 约 51.23 GiB；它们受 TCC 拒绝访问影响，都是已访问部分的下界。受限子树标为 permission unknown，不把未知项算成零。`/private/var/folders`、db、vm 和系统管理缓存没有新增 root 清理规则。

首次 df 的 APFS 共享可用空间约 62.2 GiB，后续 df 约 74.18 GiB。调查期间 Library/Caches 曾不存在，后续又出现约 411 MiB，主要为 Zotero 缓存；各次观测分开保存，没有把它们相加或当作本轮释放量。此前的飞书更新约 2.45 GiB、飞书日志约 478.72 MiB、VS Code VSIX 约 620.56 MiB 等热点已有规则覆盖，不因占用大而重复加规则。

六条新增项均为实测存在的 Chromium GPU/Shader 缓存：`targets = "dirs"`、`depth = 1`、`minAgeDays = 30`、`recursiveAge = true`、`optionalRoot = true`、`action = "quarantine"`，并要求对应应用退出。`protectedOverrides` 仅授权表中根目录下逐个列出的精确子目录。路径由机器 home 推导；下表相对 `~/Library/Application Support/`。目录类型依据已知 Chromium 缓存结构与本机元数据确认，本轮未做删除/重建实验。

| 稳定 ID | 根目录 | 精确 names | 调查时分配占用 |
|---|---|---|---:|
| vscode-gpu-cache | Code | GPUCache、DawnGraphiteCache、DawnWebGPUCache | 6.64 MiB |
| codex-shader-cache | Codex | GraphiteDawnCache | 12.13 MiB |
| chrome-shader-cache | Google/Chrome | GrShaderCache、GraphiteDawnCache、ShaderCache | 13.26 MiB |
| feishu-shader-cache | LarkShell | GrShaderCache、GraphiteDawnCache、ShaderCache | 14.32 MiB |
| feishu-iron-shader-cache | LarkShell/iron | GrShaderCache、GraphiteDawnCache、ShaderCache | 14.27 MiB |
| steam-shader-cache | Steam/config/htmlcache | GrShaderCache、GraphiteDawnCache、ShaderCache | 7.86 MiB |

六项合计约 68.48 MiB；仅 Chrome 两个目录达到年龄条件，约 1.07 MiB，且当前进程条件仍阻止执行。其它目录整树最新写入约 1.1 天以内，不能计入年龄候选。共享根缺失时可跳过；目录权限未知、符号链接、重叠候选及保护校验仍拒绝。

VS Code 既有覆盖仍仅限 CachedExtensionVSIXs（含可选 .trash）、Cache 的 Cache_Data/No_Vary_Search、CachedData、logs 和 ShipIt。新规则只加表中 GPU/Shader 目录。未把安装中的 `.vscode/extensions`、`.codex` 会话/状态、聊天数据库、浏览器 Cookie/账号数据库、Mail、Messages、Keychain、Docker volumes 或业务数据纳入默认清理。

Chrome Service Worker 约 926 MiB 可能含离线应用状态；约 88 MiB updater 根无法证明失效；系统 Container、产品持久缓存和 Zotero 缓存根也没有扩大授权。Linux 不复制未经本机验证的 Darwin 应用缓存目录。

## 原生维护

`envy storage maintain` 默认仅展示命令和可用性；没有可靠 dry-run 的原生命令不会作为预览运行。执行还需机器 policy 选择和显式 `--yes`，这些动作不进入 storage 定时任务。Air 当前选择以下七项，最新只读预览全部可用：

| 动作 | 拟执行命令 | 范围 |
|---|---|---|
| Nix | `nix-collect-garbage` | 保留 generations 和 GC roots；Darwin 用 `sudo -n -H`，需已有 sudo ticket，不读取口令或交互提权 |
| Docker | `docker builder prune -f --filter until=336h`；`docker image prune -f --filter until=336h` | 两步均保留 336h 年龄边界；镜像按创建时间过滤且仅限 dangling；不清 volumes、容器或 tagged images |
| Go | `go clean -cache` | 整个 build cache，无年龄边界，不清 module source cache；选择它会自动排除 go-build-cache 年龄规则 |
| uv | `uv cache prune` | 使用原生可达性与占用检查，不加 force/ci，不声称遵循规则年龄 |
| npm | `npm cache verify` | 会写入并回收缓存，预览不运行它 |
| pip | `pip cache purge` 或探测后的 `pip3 cache purge` | 整个缓存，无年龄边界；仅显式执行 |
| Homebrew | `brew cleanup --prune=30` | 涉及旧 keg、锁和下载；30 天不是全部对象的统一年龄边界 |

Homebrew 子进程设置 `HOMEBREW_NO_AUTOREMOVE=1`、`HOMEBREW_NO_AUTO_UPDATE=1`。只读版本/帮助核对：Nix 2.33.1、Docker 29.8.2、Go 1.26.4、uv 0.11.20、npm 10.9.8、pip3 21.2.4、Homebrew 7.0.13；Nix help 因缺 man 页面返回 16，其余相关帮助可用，版本是当时 PATH 的观测值。

Docker client/server 均为 29.8.2（Server API 1.56）。`image prune --help` 确认支持 until，官方 [image prune 文档](https://docs.docker.com/reference/cli/docker/image/prune/) 确认接受 Go duration（如 336h），相对于 daemon 时间筛选创建时间更早的镜像，不以最近使用时间计算。审计补回镜像年龄过滤后，已有回归在旧命令上实测失败、修复后通过；预览与实际调用两处均断言年龄参数，真实 prune 从未运行。

Darwin 的 pip3 使用 xcrun，devShell 的 DEVELOPER_DIR 曾导致它误选没有 pip 的 Nix Python。pip 探测和执行共用独立环境，移除 PYTHONPATH/PYTHONHOME，Darwin 额外移除 DEVELOPER_DIR；保留 PATH、PIP_CACHE_DIR 等配置且不改变父进程。回归已红绿验证，实际纯预览从 available=false 变为 true。

`storage gc` 保留兼容，但会删除旧 Nix generations，并可能调用旧 Homebrew 清理流程；它会明确警告。只需无引用 store 回收且保留 generations 时使用 maintain --nix。多步骤原生动作失败后停止其后续子命令，JSON 只报告准确返回码，不混入命令输出。

## 年龄安全回归

`StorageAgeBoundaryTests` 新增六项隔离回归，将 envY 与真实 organize 子进程同时固定在 Unix epoch 1,700,000,000，以 30 天阈值前一秒、恰好阈值、后一秒验证文件和递归目录。子进程只在临时 fixture 中冻结 time 与 Arrow 时钟；不改生产年龄代码。organize 原生文件 older 是严格比较，文件恰等阈值会保守跳过；envY 复核与递归目录允许年龄等于最小值。所有路径都拒绝未到阈值的条目。

另在规划前构造 mtime 已旧的父目录，分别让嵌套文件或嵌套目录保持新鲜，并放入一棵全旧子树作为阳性对照。一条回归检查真实 organize 的原始候选消息和最终 plan，另一条故意让扫描器报告全部目录，确认 envY 的独立年龄复核仍拒绝新子项。两个隔离故障注入分别绕过 organize 递归过滤、将 envY 检查降为只看父目录 mtime，均能触发对应测试失败；原实现六项全过且无跳过。已有“生成 plan 后新增文件”的完整性回归继续保留。既有真实 organize/restic 临时往返还补了错误仓库口令与不存在仓库的实际失败场景，确认源内容保持不变，恢复正确配置后再完成备份、释放、恢复与 read-data 校验。

专用 storage check 提供 organize/restic，111 项全部实际执行；完整 Python check 不提供这些原生工具，其新增三项 organize 场景按声明跳过，另由 storage check 覆盖。1015 项完整 Python 的实际 skipped 数量记录于日志。证据为 age-regressions.json、age-regression.log、两个 age-mutation 日志及 age-verified-checks.json。

## 挂载身份与 SFTP 连接边界

绝对路径仓库现在必须声明 archive.mountSource，值为操作系统挂载表中的精确来源标识；旧配置需补齐后才能继续使用。以下仅示意，不代表本机已有挂载：

```nix
envy.storage.archive = {
  repository = "/Volumes/share/envY/archive/example";
  mountSource = "//user@nas/share";
};
```

每次 restic 调用前先找仓库最近的挂载祖先，再用 macOS 的 /sbin/mount 或 Linux 的 /proc/self/mountinfo 核对来源。未挂载、同一位置换成错误共享、来源不唯一、权限或探测失败均在 restic 启动前拒绝，不退回本地同名目录。解析支持空格与挂载表 octal 转义；Linux optional fields 不影响字段定位。mountSource 是非敏感标识，Nix/Python 拒绝控制字符和 authority 内嵌密码。SFTP 应保持该字段为空，status 显示的是配置值，不会自动探测挂载或远端仓库。

SFTP 的普通命令和流式 dump 共用参数构造，向 restic 传入 sftp.args：BatchMode=yes、StrictHostKeyChecking=yes、ConnectTimeout=8、UpdateHostKeys=no、PreferredAuthentications=publickey、PasswordAuthentication=no、KbdInteractiveAuthentication=no。使用现有密钥/SSH_AUTH_SOCK，不自动追加主机密钥；清除继承的 RESTIC_* 后仅重建仓库、口令文件与缓存目录。已核对锁定 restic 0.18.1 的 buildSSHCommand 源码确认参数转发，不依赖单独探针才设置安全参数。

25 项后端 fixture 覆盖真实格式的 macOS/Linux 挂载表解析、正确/错误/消失/未知来源、权限/超时、认证和失败脱敏，以及 command/dump 两条路径的 SSH 参数断言。模型另验证来源条件、非敏感约束及指纹变化；配置写入流程按 FieldDef.condition 验证条件必填，四项隔离回归证明 SFTP/未配置仓库不误要求来源，绝对路径仓库缺来源会拒绝。所有这些 fixture 均不连接真实 NAS。

## NAS 配置与当前状态

机器仓库现配置为：

```text
sftp:xikai@192.168.255.139:/vol1/1000/share/envY/archive/macbook_air
```

`nas.fl0wer.cn` 当前解析到公网地址，SSH/SFTP 探测在认证前超时；原私网地址 192.168.255.139 的 22 端口可达。IP 和域名均有已有主机记录；私网直连使用 BatchMode=yes、StrictHostKeyChecking=yes、ConnectTimeout=8 和现有 SSH 密钥，验证为 xikai，SFTP cd/pwd 成功。没有接受新主机密钥或修改 SSH 配置。该配置依赖当前私网可达性；跨网不可达时应准确失败。

/vol1 是约 1 TB 数据卷，探测时空闲 973,653,819,392 字节（约 907 GiB）；真实共享父目录是 `/vol1/1000/share`，当前用户的读/写/执行权限检查均通过，`/vol1/share` 不存在。旧“/vol1 不可读”的记录已失效。本机 `/Volumes/share` 当前未挂载；没有选择系统 home 下约 50 GiB 可用的目录，也没有依赖 SMB 本地路径回退。先前的父目录权限是 os.access 只读检查；本轮 restic init 已实际创建专用仓库。事后 SSH 元数据确认路径链无符号链接、realpath 与配置相同，且仓库与共享父目录位于同一数据卷。

.nas_cred 位于仓库根，0600、gitignored、untracked，仅用于所需登录信息；登录认证实际使用 SSH 密钥。restic 加密口令独立随机生成，经现有 secret_store/sops 写入 `storage/archive/password`，加密后读回确认其它 secret 值不变。口令没有出现在参数、Nix 表达式、日志、plan、测试、本文或 goal 记录中。

当前运行时口令文件存在、权限 0400，实际 restic 已用该文件成功解锁；本轮未修改口令或运行 apply。status 的 archive configured、passwordFile 和 repositoryChecked 分开表示配置、文件状态和仓库检查，不能由 configured=true 推断备份成功。

此前 14:41 和 16:11 UTC 的只读探针均返回 10。当前目标在本轮预检仍未初始化；用户通过 questionnaire 明确批准完整实施计划后，于 2026-11-11T17:13:20.781978+00:00 完成 `init --repository-version 2`，创建专用仓库。

本轮实跑完成于 2026-11-11T17:16:15.919749+00:00：

1. 在 `~/.cache/envy/storage-archive/mwccr5w2/fixture-input/` 创建两个各 1 MiB 的合成文件，只有合成文件的 mtime 调整为 31 天前；不改变全局时钟或真实文件。
2. 使用独立 `state/`、仅包含 `archive-drill-mwccr5w2` 的内存政策和真实 organize，validate/plan 恰好选择这两个文件、2,097,152 字节，无 blocked/skipped。
3. 真实 `lifecycle.execute` 完成 NAS backup、逐文件流式 dump SHA-256 校验，然后仅将这两个生成文件 rename 为 `archived-held`。记录保留期仍为 30 天；本轮未释放 held。
4. 保留 held 原样，直接调用 `Archive.restore`，从真实 NAS snapshot 恢复到新的 `restore-from-nas/`，不调用优先本地 held 的 lifecycle.restore。两个恢复文件的 SHA-256 和大小均与归档前基线一致；包含空格和方括号的文件名也实际恢复通过。
5. `restic check --read-data --no-cache` 检查整个专用仓库通过。仓库现有一个合成测试 snapshot；未 purge、forget 或 prune。

| 实测标识 | 值 |
|---|---|
| Repository ID | `57d56514ace776480e066760dd445e2c95fa5d279f5d8871796605d4d8d7f4c9` |
| Snapshot ID | `6d79e84eecbd7823a67521d0bd4b7c7ea522a807717015f2da5c524976bb5d55` |
| Run ID | `a89cc75693934a018a9f5a1c9a7d10d0` |
| 输入/恢复数据 | 两文件，合计 2 MiB；哈希、大小与 read-data 均通过 |
| 保留材料 | NAS snapshot、本地 state/held 与恢复后的测试副本；主机默认 state 的 runs 仍为 0 |

当前 CLI 的 plan/run 没有单 rule 或独立 state 参数，因此本次通过现有 Python API 限定选择范围，避免对机器已有用户候选执行 `storage run --yes`。没有为演练增加生产 CLI 功能。NAS 约 907 GiB 可用容量和私网可达性是探测时状态；此次小规模演练验证功能，不代表大规模归档性能或跨网可用性。真实 NAS 断网/丢失场景未做注入，相关保留安全由已有隔离回归覆盖。

主机 `storage status` 的 `repositoryChecked=false` 只表示该快速命令本身未探测远端；本轮独立的真实 backup/restore/check 已成功，不能把这个标志解释为失败。`nas` 旧 SSH alias 的用户/端口与此方案不同，不用于该仓库。

## 验证结果与工作区限制

本轮使用原工作区的标准 Git flake，以下检查均通过；完整命令和退出码保存在本轮 `verification.json`。源码/规则通过后更新本文的结果文字，没有改动已验证的 Python 实现。

| 本轮验证 | 结果 |
|---|---|
| 标准 storage status / validate / plan / maintain --json | 25 条规则，34 根配置；完整逐规则报告；7 项维护仅预览，execute=false；主机默认 state 为 0 runs |
| 真实 NAS 合成数据往返 | init v2、2 MiB backup、流式 SHA-256、NAS restore --verify、hash/size、check --read-data --no-cache 通过 |
| .#checks.aarch64-darwin.storage-rule-selection | 通过；默认选择、覆盖/排除、来源、保护授权、平台与挂载来源约束 |
| .#checks.aarch64-darwin.envy-storage-tests | 111 项通过，无跳过；实际 Nix build log 为 OK，含真实 organize/restic 隔离测试 |
| .#checks.aarch64-darwin.platform-option-boundaries / envy-lint | 通过 |
| nix run .#envy -- check --all | 三台目标通过，仅求值，无 apply |
| nix flake check --no-build --all-systems | 通过；Linux 仅求值，未在本机构建或运行 |
| git diff --check | 通过 |

上一轮共享规则重构的完整 Python 检查为 1015 项通过、skipped=47，machine-config/structured-output 定向为 18/5 项；secret-safety、GitHub Actions 与标准 CLI help 等也通过。这些是上一轮的结果。本轮只改六条 Nix 规则和本文，按影响范围复核上表入口，没有把历史完整 Python 数量当作重新运行结果。

原工作区曾受另一个会话的两个未跟踪源码影响：`modules/agents/providers/codex/models.nix`、`resources/scripts/envy/agent_codex.py`。缺失前者使 Nix 求值失败，缺失后者使 CLI 导入失败。先在 checkout 外按 Git tracked/unignored 源文件生成隔离 Git 快照，排除 .nas_cred 与构建缓存，完整验证通过。随后用户单独批准仅将这两个既有源码加入原索引；没有修改其内容、commit 或 push。原工作区各轮复核记录在 approved-checks.json、age-verified-checks.json、nas-boundary-checks.json，保留初次失败和隔离快照结果以便区分验证来源。

上一轮新增的 8 个源码/测试文件已按授权加入原工作区索引，便于标准 flake 读取；没有 commit/push。其它会话还修改了 agent 配置及 host 的非 storage 字段，本任务保留这些内容。所有本任务测试、构建与证据缓存均在 checkout 外；其它会话曾在 checkout 中生成 target，未删除或移动它。

上一轮私有证据在 `~/.cache/envy/storage-refactor/mwc6h7p0`：baseline.json、migration.json、migration-comparison.json、final-live.json、nas-probe.json、nas-config.json、restic-readonly.json、host-checks.json、final-checks.json、snapshot-checks.json、approved-checks.json、post-audit-checks.json、docker-age-support.json、docker-age-preview.json、age-regressions.json、age-verified-checks.json、nas-boundary-checks.json、nas-boundary-live.json、nas-boundary-comparison.json、nas-sftp-boundary-readonly.json 与检查日志。目录 0700，私有 JSON 和日志 0600，不含密码值。原 ncdu/organize 探针亦保留于此前临时审计目录。


本轮私有证据位于 `~/.cache/envy/storage-archive/mwccr5w2`：`git-baseline.json`、`rule-baseline.json`、`disk-audit.json`、`privileged-disk.json`、`rule-proposals.json`、`nas-preflight.json`、`archive-initialized.json`、`archive-drill.json`、`archive-drill-plan.json`、`archive-drill-run.json`、`nas-after-drill.json`、`final-reports.json`、`rule-report-final.csv/json`、`report-comparison.json`、`verification.json` 与 `storage-test-build.log`。目录 0700，私有证据文件 0600。

## 本轮与先前改动范围

本轮仅修改 `modules/storage/rules/darwin.nix`（追加六条已审核规则）和 `docs/storage.md`；使用既有 API 完成演练，没有改 Python/CLI、机器模式或 sops 内容。没有新增暂存、commit 或 push。此前已暂存的文件和其它会话改动保留。

下表是上一轮实现保留的文件范围：

| 文件 | 作用 |
|---|---|
| modules/storage/options.nix、rule-options.nix、rules/{common,darwin,linux,default}.nix | include/exclude/effective、字段类型、共享默认、精确授权与来源 |
| hosts/darwin/macbook_air.nix | 移除完整规则/全局授权长列表，保留模式、维护与 NAS 差异；其它会话的 host 改动保留 |
| resources/scripts/envy/storage/{model,planner,lifecycle,cli}.py | 有效契约/兼容、元数据、缺失与权限、冲突、旧计划、JSON/status |
| resources/scripts/envy/storage/{maintenance,backend}.py | 原生维护、pip 子进程环境、挂载来源身份、统一 SSH 参数、后端错误与不完整备份拒绝 |
| resources/scripts/tests/test_storage.py、test_storage_backend.py、test_storage_maintenance.py、test_machine_config.py | 选择/安全/隔离往返、NAS 失败、维护与 managed block 回归 |
| checks/storage-rules.nix、flake.nix、.github/workflows/envy-checks.yml | Nix 选择回归与 CI；storage check 纳入所有 storage 测试 |
| resources/scripts/envy/schemas/common/config.py、config/refine.py | 非敏感挂载来源字段与条件必填验证 |
| secrets/secrets.yaml | 独立 restic 口令的 sops 密文，其它 secret 值读回保持一致 |
| docs/storage.md | 当前架构、覆盖、真实只读结果、限制与后续关卡 |

## 后续独立授权关卡

专用仓库初始化和本轮合成文件的 NAS 归档/恢复已获授权并完成，后续不再将它们列为未验证项目。当前剩余关卡：

1. 单独选择真实用户数据的文件型 archive 规则；当前没有把缓存统一改为 archive，目录 archive 仍未支持。
2. 分别审核真实清理、auto/定时任务和 apply。应用退出后重新 plan，逐项审阅，再决定 run/purge/native maintenance 的 --yes；本轮没有获得这些执行授权。
3. 若需要大规模 archive、跨网运行或真实 NAS 故障演练，另定路径、数据量、恢复与保留策略。当前测试 state 没有接入主机调度；测试副本和 snapshot 的删除也应明确范围，不自动 forget/prune。
