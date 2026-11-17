# TUI 前端边界

启动：

```bash
envy tui
```
Rust/Ratatui 前端只负责展示和交互，通过 envY CLI 的 JSON boundary 调用后端；policy、事务、镜像、软件来源和 secret 规则仍由 Python/Nix backend 所有。

## JSON 约束

- 请求和响应使用 resources/scripts/envy/jsonio.py 的稳定 envelope。
- 前端不能直接读写 host、source、lock、secret 或应用配置。
- 失败必须保留 backend 的错误码和可操作信息；不能把 secret-safe 状态扩展成敏感值。
- 新命令先实现 CLI，再接入 TUI；两者不得各自实现 policy。

```bash
cargo test --manifest-path resources/scripts/envy-tui/Cargo.toml
python3 -m compileall -q resources/scripts/envy
```

## 共享调用与部分失败

`backend::run_json_response` 返回完整 JSON、进程退出状态和有界 stderr。有效 JSON 即使伴随非零退出也保留；报告消费者必须继续展示可用数据、逐项错误和 warnings。`ok: false` 不表示操作成功，不能用部分结果进入下一次写入确认。

变更调用使用严格成功路径，并验证完整 envelope 和请求身份。协议错误、进程启动失败、超时、输出超限及业务失败分别报告。现有只读顶层 JSON 保持兼容；新增 envelope 校验精确的 `command` 和协议版本。请求默认限制为 120 秒、stdout 8 MiB、stderr 256 KiB；超限或超时终止该请求的进程组，结果需要重新核验。Plan 构建使用独立的 2 小时、stdout 16 MiB、stderr 4 MiB 限额，并增量显示 stderr 阶段进度；取消同样终止该请求的进程组。

`config show --json` 的 `data` 包含完整配置视图，同时保留旧顶层键。字段说明、类型、choices、required、适用状态和生效提示由 Python 提供；条件缺少输入时为 unknown。secret 元信息只包含设置状态。agent 字段的生效提示复用现有 setter 的 live/apply 分类。字段的可选 `setter` 指明写入入口：`config.set` 或 `agent.set`；secret 为 null。前端沿行、编辑和确认保留该标记，Profile 使用 agent setter，与即时刷新提示一致。旧响应缺少标记时沿用兼容路由，未知或明确不可写的标记不进入写入。

`storage capabilities --json` 提供动作目录、政策 opt-in、参数及副作用说明，不扫描目录、不探测 Docker、不加载归档密码。覆盖标记区分原生交互、CLI 交接及后台政策；CLI 交接不表示原生执行。前端维护选择默认空集合，空选择不得回退到 CLI 的全部已启用动作。

## Settings 展示

Settings 优先显示后端字段说明，并保留真实配置路径作为编辑身份。同名说明不会合并字段。选中行的帮助区展示分组、类型、必填与适用状态，以及 setter 提供的生效提示。条件为 inactive 或 unknown 的字段仍保留，前端不自行重新求值。

secret 行只读取 `set` 状态，忽略 value 和 choices，且不能进入编辑确认。枚举字段继续使用下拉选择，普通字段继续使用文本输入；旧响应缺少元信息时回退为路径标签。窄窗口保留字段和值，表格滚动按帮助区之外的实际高度计算。

## Plan、History 与执行记录

Plan 入口调用 `plan --json`。比较报告分别展示 closure、机器值、软件选择、来源及已声明 activation 动作，并保留各部分的 `compared`、`unknown`、`unavailable` 状态与原因。closure diff 失败保持 unavailable，旧 generation 没有已绑定语义基线时保持 unknown。声明动作和软件选择不代表运行态已兑现，管理器的回滚边界由后端说明。

报告中的实际构建来源来自冻结的 Nix source；Git revision、dirty 指纹、lock 指纹、NAR、drv/output 各自保留含义。Plan 默认不读取应用配置正文，运行态 merge 预览缺证据时保持 unknown。默认人类 CLI 输出仍可用，JSON envelope 同时保留旧顶层字段；机器可读输出不经过终端颜色或换行处理。

History 的 Enter 打开 generation 详情，Space 标记后用 d 比较；Journal 的 Enter 打开 receipt。详情保留 source、前后 generation、阶段、终态、后续核验提示与完整原始 JSON。有效的非零响应、partial、warnings 和 stderr 保留；没有行的失败报告也可打开。旧裸 JSON 仍能查看，长路径与大报告可以完整滚动。

Journal receipt 的 x 仅为后端允许的 `verify-generation` 打开确认，执行固定的 `log --retry ID --step verify-generation --json`。该步骤重新核验记录与当前机器、profile、generation、来源的关联；不重放 apply 或外部管理器命令。未允许、未知及后端拒绝的原因保留在界面，取消和重复确认不重复提交。缺终态的操作显示 interrupted；generation 回退不保证恢复 Homebrew、系统偏好或全部外部状态。

此阶段 CLI/TUI 实现已通过标准 Git flake 检查与独立审查。宿主 apply、Linux VM 与真实运行态证据按后续运行验收阶段记录。

## Storage 只读视图

从 Home 按 `t`，或在 Activity 用 `v` 切换到 Storage。首次只请求 `storage inspect --json`。使用 `[` / `]` 显式切换盘点、政策与记录、规则验证；记录视图可能初始化和锁定本地状态，规则验证会运行 organize check 并使用临时配置。进入页面、切换顶层页和查看详情不会生成清理计划或执行维护。

在 Storage 按 `p` 添加一条完整项目路径，`x` 移除最后一条，`X` 清空。每条路径独立作为 argv 值传给 CLI，空格、中文和 shell 字符不作为命令解析。Enter 打开详情与完整 JSON，可用方向键、PageUp/PageDown、Home/End 滚动。

缓存按报告视图和项目范围区分，最多六条，显示接收时间，五分钟后标为 stale。显式刷新失败时保留旧报告并显示错误；过期请求不能覆盖新请求。开始写入设置时会使 Storage 缓存及在途请求失效，同时关闭旧报告详情，避免混用机器政策。写入失败可能已有部分声明或运行态变更，错误保持可见，旧缓存不继续当作当前政策；失败不自动触发 Storage 扫描或 Plan。部分失败报告保留已成功项、warnings、逐项 errors 及进程状态，不显示成完整成功。

逻辑量、分配量、原生可回收量和政策候选量分别展示，未知量保持 unknown。路径不存在的 0 与无权限的 unknown 有不同语义；目录包含、APFS 共享块和 Docker 共享层不能直接相加，quarantine 也不表示宿主空间已经释放。Python CLI producer 与 Rust consumer 共用合成 golden fixture，命令或字段变化时应同时更新两端契约测试。

## Storage 预览与确认

Storage 的 `a` 打开操作页，仅加载 capabilities；`s` 才显式扫描并保存 plan。Tab 切换操作，`i`、`r`、`t` 分别输入已保存的 plan ID、run ID 和恢复目标。维护动作使用后端目录中的八项选择，初始全部未选；空选择不发请求，Go module cache 仍要求独立政策 opt-in。

`p` 请求后端预览，Enter 打开确认，`y` 提交同一请求的 token。取消、修改输入或选择、切换机器、开始写入设置都会丢弃旧预览；旧响应不得覆盖新上下文。后端校验 token 的 300 秒期限、计划来源、政策、源码、宿主及实际工具目标，任何漂移都要求重新预览。前端仅传固定 argv，不解析 shell、不读取目标文件、不计算清理资格。旧后端没有确认协议时，操作页显示不可用。

后端先持久化单次 claim，再调用既有引擎。重复确认返回原结果；claim 后中断保持 unknown，不重放操作。已消费记录的结果读取与新的执行授权分开，不能因后续政策或源码更新丢失原结果。执行取消只表示已请求停止；外部命令可能已经产生效果，界面保留 unknown，不宣称回滚。失败、已知成功项、逐项错误、stderr、退出状态及后端 refreshedStatus 都可查看，不自动追加 inspect、plan 或重试。

恢复预览不创建目标，远端证据校验可能读取归档、需要凭据并耗时。确认恢复只写新的绝对目录；通过目录 FD、独占新建及输出身份复核防止覆盖竞争文件。失败可以留下新建的部分目标，不能把部分数据说成完整恢复。操作使用独立长请求限额；普通只读请求限额保持不变。

`h` 展示 init/check/gc 的完整命令、目标、参数、退出方式与副作用。打开、浏览和确认交接说明均不启动命令；用户退出 TUI 后自行运行。下表区分原生交互与 CLI 交接。

| CLI 能力与 flags | 当前 TUI 覆盖 |
|---|---|
| capabilities --json | 原生动作目录及交接元信息；不扫描、不探测工具 |
| inspect --project PATH --json | 原生只读；项目路径可重复 |
| status --json | 原生按需读取，可能初始化和锁定本地 state |
| validate --json | 原生规则核验，使用临时配置，不生成 plan |
| plan --json | 原生显式保存；进入页面不生成 |
| run PLAN --preview / --preview-token TOKEN --yes --json | 原生预览确认；必须显式指定已保存的可信 plan |
| maintain --nix/--docker/--go/--go-modcache/--uv/--npm/--pip/--brew --preview / --preview-token TOKEN --yes --json | 原生非空多选及绑定确认；flags 表示可单选或组合 |
| purge --preview / --preview-token TOKEN --yes --json | 原生预览确认；固定到批准的条目，不纳入后来到期项 |
| restore RUN --target ABS --preview / --preview-token TOKEN --yes --json | 原生预览确认；旧 CLI 不带新协议时仍立即恢复，TUI 不走该路径 |
| init --json | CLI 交接；初始化专用归档仓库 |
| check --json | CLI 交接；完整归档数据读回，可能产生网络与本地 cache I/O |
| gc --older-than 30d --brew/--no-brew --yes | CLI 交接，无 JSON；删除旧 generation 并清理缓存 |
| run --scheduled / report mode | 后台政策或模式，不作为普通 TUI 操作 |

Python producer 与 Rust consumer 共用 `src/storage/fixtures/operations-v1.json` 的 capabilities、维护预览、恢复预览和 partial result。flags、字段、覆盖或副作用改变时须同时维护 producer、fixture、Rust 消费与交互测试。本阶段已通过标准 Git flake 的 Python 1347 项（47 跳过）、Storage 331 项（无跳过）、Rust 184 项（无跳过）、Codex 27 项（1 跳过），以及 lint、agent 和平台边界检查；独立复核确认已报告问题闭环。这些是隔离测试和构建证据，宿主清理、恢复、NAS 写入和调度未在本次升级执行。

归档 `storage check` 已在后端固定执行 restic `check --read-data`，不需要再给 envY CLI 传 `--read-data`。`gc` 删除旧 Nix generation，与保留 generation 的 `maintain --nix` 不同。

## Habit 与 Doctor 核验

习惯与偏好的回读由 Python/Nix 提供，使用 habit check --json 或 doctor habits --json。两者保留旧顶层字段，并提供 schema 1 envelope；默认 Doctor 的旧 JSON 形状兼容。Doctor 使用同一比较器和证据，前端不根据进程、配置文件或字段自行推导生效状态。

details.verification 分开保存 declaration、stored、runtime、pending，以及原因、来源、范围、取样时间和声明指纹。Darwin KeyRepeat/Fn 与 GNOME Tilix binding 只证明保存值；Niri 的原生 IPC 只证明运行中 compositor 报告的布局名称与索引。缺少可靠重新登录或重启信号时保持 unknown，应用拥有的绑定仍人工检查。适配范围、命令和所有权边界见 [交互习惯与偏好核验](habits.md)。

阶段 7 的合成 Python/JSON/Doctor 定向测试 135 项通过；标准 Git flake 的 Python 1389 项（47 跳过）、Storage 331 项（无跳过）、Rust 184 项（无跳过）、Codex 27 项（1 跳过）及 lint、agent 检查通过。真实个人偏好回读、宿主 apply 和 Linux VM 不属于这些结果，按运行验收阶段记录。

## 运行验收与来源边界（阶段 8，进行中）

运行验收分开记录合成测试、Nix 求值、产物构建、可丢弃 VM 和真实宿主结果。测试数量、工具安装成功或 QEMU 正常退出，都不能替代 guest 内完成操作的回执。

来源回归使用独立临时 Git 仓库、HOME、配置和 Nix store，覆盖 tracked 修改与删除、untracked import、intent-to-add、ignored 文件以及冻结 NAR 与文件清单。host import/copy 通过生产创建和 managed-block 接口生成，再由真实 Nix module 求值验证默认文件变化后的差异；提交跟进固定拒绝，不进入生产提交检查。CI 继续使用已固定 SHA 的 actions，并通过离线 GitHub Actions 检查。

交给 VM 的源码来自受限导出：只复制允许的 tracked/intent-to-add 当前内容；实际机器文件、secrets、设备标签、Git 历史、agent 状态和缓存在读取前排除。合成默认机器保留完整软件组合，仅求值；最小机器从已求值 manifest 生成字面量 exclusions，供运行测试使用。导出和最小选择更新必须通过目录 FD、链接拒绝与身份复核保护，不能将路径竞态后的重新校验当成撤销写入。

Linux 运行验收按以下顺序收集证据：

1. 固定 SHA256 的官方镜像在 QEMU TCG 中启动，回执证明 x86_64 和初始未安装 Nix；首次启动探针关闭网络、共享目录、转发、GUI 和 monitor。
2. 仅传入批准源码与固定 Nix installer，在 guest 内原生获取锁定的公开依赖、构建 Linux 产物、生成合成身份及 age/sops 数据，再完成首次配置与 apply。完整验收的 guest-fetch 路线使用固定 user-mode NAT，无宿主目录挂载或端口转发；保留 TLS、锁定 hash 与缓存签名校验。
3. 重复 apply，比较 generation、托管文件内容与权限，并单独记录不可重复的外部管理器步骤。
4. 在合成托管文件或受控执行步骤注入失败，核对错误、partial/unknown、现场保留及恢复后核验；不以重新执行掩盖第一次失败。

Darwin 定期验收设计采用人工触发，频率为每次发布前及每月一次；目前未启用调度，也未执行真实宿主 apply。以下步骤须在明确授权的可重置 macOS 测试机或 VM 上运行，使用合成用户与数据。机器值仍只放在选定的 Darwin host module；不能把 Linux fixture 的路径或平台政策直接当成 Darwin 配置。

1. 记录目标机器、macOS/Nix 版本、源码及 lock 指纹和当前 generation，在 checkout 外私有目录保存状态、history、log 与 plan JSON。先核对 plan 中实际 Homebrew、cleanup 和系统偏好动作的作用范围；真实 apply、原生清理或恢复覆盖分别取得授权。只留允许的测试路径和状态摘要，不采集应用正文、认证材料或整个 home。
2. 在固定源码上通过 Darwin 的 Python、Rust、storage、agent、lint、平台边界、来源和 CI 离线检查，并求值两个既有 Darwin 目标。求值成功、构建成功和 activation 成功分别记录；跳过或 unavailable 不能计入通过。
3. 在测试机执行首次真实 apply，保存退出码、逐阶段 journal、终态持久化确认、目标 output 与实际 generation binding。对合成托管文件核对内容、权限及非托管键保留；secret 只记录是否存在和权限是否正确。成功终态或 generation 单项存在都不足以证明整条链路通过。
4. 原样重复 apply，核对目标 closure、合成托管文件与权限；generation 允许复用。分别记录外部管理器的重复执行与副作用，不能仅凭 generation 不变判定全部幂等。
5. 仅对测试机内合成托管文件注入并发修改，确认冲突停止、保留现场与 partial/unknown；故障恢复后重新核验 source/output/generation，且旧失败记录仍可读。强制 activation 失败只在可重置环境单独授权后执行，不在日常宿主制造故障。generation 回退不代替 Homebrew 安装、cleanup、系统偏好及其他外部状态的恢复核验。
6. 用已有固定 habit/doctor adapter 采样系统偏好，分别记录 declaration、stored、runtime 和 pending。stored 匹配不推导 runtime 匹配；需要人工触发或无可靠信号的操作保留 unknown，并记录人工验证范围。结果必须同时附环境、实际执行步骤、未执行步骤和残余限制，才能与 Linux VM 证据比较。

上述内容属于验收设计，不能计作 Darwin 实机通过；任何未来运行结果都须另附当次证据。

b17 及更早冻结的历史记录包含受限源码导出，以及默认和最小 Linux manifest 的真实求值；最小策略保留 15 个 Nix 软件项，其余软件组为空。目录 FD 与链接保护的原 2 项 P1、2 项 P2 已经独立静态复核关闭；37 项临时文件系统回归通过。标准 Git flake 的来源检查在 Nix 构建环境中通过 414 项合成测试和 18 项真实 Git/Nix case（45 条实际命令），CI 离线审计和平台边界检查通过。早期完整 Python 检查为 1426 项（47 项跳过），当时 lint 与三机求值通过；这些不是后续全部 delta 的最新全仓通过声明，当前各冻结的定向、标准检查与 VM 结果见下方逐轮记录。三机仍有阶段 5 已记录的 store-reference context 警告。

真实离线 VM 首启也已通过：QEMU 10.2.2 启动固定 SHA256 的 Ubuntu 24.04 minimal 镜像，guest 回执确认 x86_64、初始无 Nix，并正常关机；该过程没有宿主目录共享或网络。随后在新的可丢弃 guest 中，固定 SHA256 的官方 Nix 2.33.1 archive installer 完成离线 multi-user 安装，十阶段回执全部通过：初始无 Nix、归档校验、安装、客户端版本、daemon RPC、daemon 服务、32 个构建用户与 profile 均已验证。ISO 匿名 FD graft 也已真实运行成功。镜像具备 curl、tar、xz 和 Python，未安装 Git。

这两次历史 guest 验证分别标记为 boot-only 和 nix-install-only，不覆盖 envY 首次配置/apply、重复 apply、失败恢复、TUI 构建测试或 requires.sh 缺 Nix 时的 HTTPS 分支。Darwin 仍只有上述受控验收设计。

Darwin 原生 Nix 不支持目录 FD URI 的文件缓存 backend，试验性目录 prepare 已删除。固定 FD 流导出通过 33 项合成测试及公开小样本导出/导入；完整离线预检查询 963 次后仍有 13 个固定输出缓存缺失、48 个来源待证明及其他缺口，保持 blocked，未执行 bulk prepare。这条旧离线路线不阻止完整 VM 使用固定 NAT 原生获取锁定公开依赖。

完整 guest-fetch 验收保留 TLS、原 lock/NAR hash 与官方缓存签名，不携带 Host CA、代理、凭据或 home。Linux 的 atime 稳定字段比较、根设备身份、早期 guard 回收和直接串口回执已修复并分别审查。第六、七个 VM 的 Guest 系统 CA、三个公开端点及 Nix 官方 cache 原生请求均通过，Nix 单 setting 与系统 bundle 一致，但原 archive 仍有证书错误；宿主公有 CA 对照显示 GitHub 主站校验失败、API/codeload 可用。因此增加按原 revision 与 NAR hash 的官方 codeload 预取，保持原 archive 与 lock 校验。

第八个 VM 已实际通过 Linux Nix 2.33 expected-hash 语法及多次 Native NAR 复核，随后在单输入 120 秒预算处超时。磁盘日志缓冲曾导致阶段误判，现已改为及时 flush，25 项 Full 定向测试通过。最新预算与进度实现将单输入上限改为 600 秒，保留阶段 1800／全程 7200 秒；仅输出 nonce 绑定的数字计数与 start/verified 状态，357 项离线回归通过。第九个 VM 已完成 23/23 个 GitHub 锁定输入复核，原始 archive 与 setup 运行时 build 均 exit 0，随后 locked-tools 表达式 build 退出 1。宿主离线只读探针复现 bare-path getFlake 的未锁定引用错误；绑定已验证 NAR hash 后，相同四工具表达式纯求值通过。该最小修复通过红绿回归、357 项标准来源测试和独立审查；第十个 VM 已通过 Linux 原生四工具 build/eval、真实用户会话、生产 Python 重入、默认完整 manifest 求值及 setup/envy 运行时构建。其后在 setup 阶段失败，正式失败帧经 Host 验证安装 nonce，但 setup 具体原因仍未知，尚未执行 apply。仅含固定错误码、对话阶段、已观察标记和计数/hash 的安全诊断随后通过 371 项离线回归（Guest 147 项）及独立审查，未记录终端正文、放宽成功条件或猜测输入；第十一个 VM 在对话 phase 1 以固定错误码 unknown-pty-prompt 停止（保存完成标记未见不等于证明未保存）；仅固定合成文本的 PTY 随后复现菜单 password: 字段标签及已回答问题重绘的误判；修复同时关闭 OSC/UTF-8/控制缓冲 EOF 绕过、菜单后重入与同批未来问题授权，保持未知新问题拒绝和按键只发送一次。既有 Guest 进程组身份 P2 也已改为 WNOWAIT 观察、stop 后再 reap。合并版本通过 398 项隔离测试（Guest 174 项）、18 个真实来源 case/45 条命令、四阶段合成 PTY（1 次菜单保存、1 次确认、2 次拒绝、真实 exit 0 与 EOF）及独立审查；这些证据尚不构成真实 Guest setup/apply 通过。

第十二个 VM 已通过全部锁定输入、原始 archive、工具和运行时构建，新 WNOWAIT 已完成 Linux 非 PTY 及真实 setup PTY 的退出与清理；setup 随后真实返回 exit 1，phase 1 仍未到保存确认，未观察到保存完成标记且尚未 apply。只读 AST 复现中公开 15 个 settings 加明确合成 secret 的 19 个适用字段均通过验证，但这不证明 Guest 实际输入相同；退出根因仍未知。新增的失败诊断仅含封闭分类、白名单异常类／调用点及 schema ID，UTF-8 内存尾巴限 8 KiB，不输出终端或异常正文；无消息异常和异常正文伪造诊断证据的两项 P2 已闭环，414 项离线测试（Guest 190、分类器 16）、标准来源 18 case／45 命令和独立静态复核通过，原有应答／成功条件／进程回收保持不变。

第十三个 VM 使用 b17 完成全部锁定输入和前置构建后，setup 真实 exit 1，封闭分类首次确认 validation-rejected／envy.git.email，未观察到 Python 异常，仍处 phase 1，未观察保存完成标记且未 apply。公开 fixture 邮箱通过校验，实际 manifest 读取回退的原因尚未确认；生产求值有 30 秒上限，但尚不能断言为超时，下一轮仅在 Guest setup child 开启既有调试功能并输出封闭求值状态／退出码，继续屏蔽正文；

b19 的求值诊断跨截断 P2 已关闭，428 项隔离测试通过。标准构建随后发现旧进程组探测 EPERM，进一步静态确认 Host／FullRunner 先 reap 再按旧 PGID 发信号的独立风险，正在改为 WNOWAIT 保留身份，并补原生 fixture 握手／held-lock 验收。首轮 445 项全套仍有 50 failures／1 error，发现 Darwin 自有僵尸组 SIGKILL 返回 EPERM，当前不计通过或启动 a14；受控探针确认 WNOWAIT 保留 leader，正实施仅查询该自有组 PID／PGID／状态的严格正面核验，查询失败或存在活跃成员继续报错。Host／FullRunner 的 owned lease 路径通过本地 465 项与独立复核，保留先 stop 后 reap。b20 标准检查的 465 项有 50 failures／3 errors：系统 /bin/ps 带 setuid 位，沙盒执行返回 EPERM；此前对自有组的系统 ps 查询已停止，后续改为锁定 Nixpkgs 的无 setuid ps。同沙盒正面查询成功，独立打包身份及重复参数拒绝修正通过本地 480 项／71.520 秒及定向 53 项，原身份 P2 已复审关闭。b21 标准 480 项仅 1 failure，确认为 fixture 在写 child.pid 前发布 ready；现调整发布顺序，新增红绿回归且 VM 定向 69 项通过。另一 Linux fixture 的 None 环境值已用既有离线、非特权、无宿主挂载容器证明旧红新绿。b22 Darwin 标准 Nix 已通过 481 项及来源检查，新来源冻结完成。Linux 单 case 已红绿通过；完整回归发现模拟 Darwin 测试在 setup 前依赖真实 ps 和 /private/tmp，现以限定该 class 的 synthetic ps／tmp 注入修正，保持生产、真实 Native 依赖和全部断言不变，17 项缺依赖红绿与 69 项 VM 回归通过。b23 Darwin 标准 Nix 完整 481 项／86.916 秒、18 个真实来源 case／45 条命令，以及既有离线非特权容器 Linux 481 项／90.196 秒（0 skip）全部通过；小增量独审未发现可操作 P1/P2。新冻结 563 文件、原 lock、7 次 native／0 次公开 metadata／无宿主 Linux 构建通过。a14 真实运行 703.359 秒后失败：Guest 独立安装回执报告十阶段完成，5/23 输入已 verified，第 6 预取发生 timeout；Host 未收到完整 acceptance 帧，partial 为 null。该轮未到 setup，未取得初始 manifest 求值证据。a15 同 b23、原预算、新 VM 再现同一第 6 Nixpkgs 原生预取 timeout，运行 952.695 秒，仍未到 setup；两轮失败均保留。宿主相同公开端点无凭据 HEAD 在 0.702 秒内 HTTP 200／TLS 0，仅为宿主可达性证据。只读调查确认现有 timeout 丢失当前命令观察。官方固定 Nix 2.33.1 的原站 raw TLS 校验失败（exit 60），改读同属官方 API 的固定 tag 文件并核对 Git blob，确认下载、解包、添加到 store 活动及 internal-json 格式。限定 Guest 两文件的封闭观察方案已独审通过，正在实施；累计 ID≤16、raw parser＋classifier 合计≤8192 字节、污染永久 unknown，仅扩静态错误码，不扩诊断字段或成功回执。Guest 两文件已完成隔离红绿：旧码235项有2 fail／44 error，新码Guest235／全runtime512全部通过、0 skip，Ruff／compile／diff通过，index与其他296个源码SHA不变；父记录进度导致docs/tui.md差异，作者未覆盖。独立实现复审发现的msg闭合schema P2已关闭：fix-02旧码5项／21失败断言红，新码同组5／Guest239／全runtime516通过、0 skip；Ruff／compile／diff／index与296个其他源码保护一致，独审无新增可操作P1/P2。第一版512及P2红绿分别保留。b24标准Nix source-tests／fullProbe通过（90.707886秒）、来源矩阵18case／45commands通过，新563文件／原lock／7native／0public-metadata／无网络或宿主Linux build冻结通过（17.377888秒）。最终来源aa6yfrlq4qybj912ciph39djifackyji-source绑定a16真实运行1045.882292秒后失败，第6项报告prefetch-timeout-active-download：下载活动未见闭合，未见unpack/store-add，仍未到setup；该码仅说明观察，不能断言网络或写入根因，不倒灌a14/a15。Host用Native规范source路径只读查询官方cache，找到与原lock同NAR hash、197465504字节及官方signature字段，未复制依赖；元数据签名存在不等于已独立密码验签。限定Nixpkgs的Guest原生官方cache获取方案已独审可实施，并明确Native CA内容信任不等于逐对象密码验签：Native copy实际返回任何错误即停止，无静默fallback；以原lock NAR→Native canonical→内容导入→path-info→既有prefetch完整同路径校验身份。已只读确认第6/7两项均有同hash官方cache元数据。Guest两文件实现已冻结：正式聚焦旧码4项／4fail／0error红，新同组4项绿；Guest249／全runtime526通过，Ruff／compile／diff／index通过，584保护文件仅父授权docs进度变化。独立实现复审通过、无可操作P1/P2，确认完整canonical/原hash/五步同deadline/前后锁复核与首错、非Nixpkgs序列和Native信任边界。Native缓存传输尚未实测；所有新增调用共享既有input deadline，预算、Native require-sigs、默认keys/CA、wire与其他Github路径不变，b25标准Nix source-tests／fullProbe通过（102.814600秒）、18case／45commands通过，563文件／原lock／7native／0public／无网络或宿主Linux build的新来源冻结通过（11.564005秒）。最终1s2j5z8j2jkwsh5mv44xadvp8ri038m6-source与新包装已绑定全新a17启动，并保留checkout外临时构建引用，未执行GC。a17真实运行723.789904秒后第6项timeout，最后成功记录为Native canonical计算，未取得cache copy完成证据，仍未到setup。官方固定2.33.1 store-api.cc源码已核TLS与Git blob：actCopyPath100绑定完整path/src/dst，resProgress105报告NAR流进度，stop或报告到总量均非导入成功。copy专用观察两文件实现已冻结并独审通过（无可操作P1/P2）：新增13定向绿、262Guest通过；首次全suite被一次性50秒harness截断保留不计通过，fix-02以现有owned-lease Runner/240秒测试期限完成539runtime／0skip／127.245秒及Ruff／compile／diff通过，产品预算未改。旧50秒遗留process状态未知、未向历史PID/PGID发信号。绑定完整canonical/src/dst，NAR进度/stop/报告总量只记observed，优先保独立exit+openstream事实，异常永久unknown。保8KiB raw／8MiB总输出／16累计IDs与原预算，真实JSON体量风险待验证，超限保原错误，不加wire字段；旧13红测含63errors，不作为有效行为红；fix-03经原prefetch_github_inputs入口取得旧码2fail／0errors→新同组2pass，源码未变。b26标准Nix（132.431956秒）／18case45commands与563文件原lock／7native0public／无网络或宿主Linuxbuild的新来源冻结（19.508424秒）通过；q16d79lmak36k10r65vkkhzgmn7m7sfn-source及新包装绑定的a18于96.561771秒失败：安装十阶段全true，native-tls command-timeout，未到输入预取/copy观察、无acceptance。保留首错后同b26/同预算与网络参数的独立a19于734.390703秒结束，第6项5/23，NativeCAsetting匹配且cacheping0，诊断cache-copy-timeout-unclosed-progress，证明绑定copy见正NARsource进度、未报告总量或stop，非copy完成/进程running/网络根因。当前独审仅copy --option http2 false的受控实验：官方2.33.1源码已核默认允许H2与false映射HTTP1.1；baseline实际协商协议未知，单轮成功不等于H2根因。尚未改代码，b27/a20未启动，TLS/签名/hash/原lock与所有预算不变；checkout外引用保留，未GC；不将a16下载活动回填a17，具体根因与setup/TUI/apply/fault97/recovery仍未知，cachehit也不能证明codeload重新下载。对本次包装与公开来源使用checkout外临时构建引用，已核包装closure包含精确QEMU，未执行垃圾回收或修改系统配置。固定QEMU恢复3在240秒超时，恢复4临时脚本转义有误而未启动Native下载；原件均保留。修正后的恢复5先编译检查、再复制同一官方签名产物，exit0／6.602715秒、精确binary存在、无source build。宿主工具恢复等待上限单列600秒，VM的600/input、1800prefetch、7200full预算及TLS、签名、来源不变。邮箱回退根因及setup／apply仍待验证。首次/重复/fault97/恢复 apply 和 TUI 构建测试仍待完整 VM 证据。

工作目录与原生 overlay/ISO 输出叶的原 2 项 P1、1 项 P2 已独立复核关闭；23 项 VM、25 项 install fake，以及锁定工具的输出 FD、三个 QEMU fdset 小探针通过。小探针未执行 guest 指令，历史 boot/install 功能结果不替代当前完整生命周期结果。
