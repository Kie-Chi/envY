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
