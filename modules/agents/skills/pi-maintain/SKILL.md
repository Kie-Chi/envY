---
name: pi-maintain
description: "维护 envY 锁定的 Pi 宿主、Nix 打包、补丁、扩展和父子 agent 工具能力时，核对版本/hash、声明与实际 API、运行时依赖，并选择现有隔离离线回归。用于 Pi 升级和集成实现；根因调查用 hunt，配置漂移审计用 health，diff/PR 审查用 check。"
---

# Pi 集成维护

先读 envY 的 `AGENTS.md`、`docs/agents.md` 和受影响文件，检查 git 状态与已有 diff。沿用已批准的计划；本技能不另设审批步骤。以下项目路径相对实际 envY checkout，不相对安装后的 skill 目录。

## 锁定源码与实际宿主

1. 从 `flake.lock` 的 Pi input 读取锁定 `rev` 与 `narHash`，通过 `inputs.pi` 定位源码，检查其 package 版本。不要以网站最新文档代替锁定版本，也不直接修改 `/nix/store`。
2. 确认本次测试用的宿主路径、包版本及是否含 `moreThinking` 补丁；当前锁定基线为 Pi 1.1.0，具体 revision/hash 以 lock 为准。升级时同步复核 `modules/agents/providers/pi/` 和 `files/agents/patches/`。
3. 完整阅读该宿主所附的相关 `docs/*.md` 与相关示例，并跟进与实现有关的交叉引用。skill 看 `skills.md`；扩展看 `extensions.md`、`packages.md`；SDK/子会话看 `sdk.md`、`examples/sdk/14-codemode-mcp.ts`；工具编排看 `codemode.md`、`settings.md`；headless 看 `cli.md`、`cli-integration.md`。终端 UI 改动另读 `tui.md`。
4. 对照宿主的 `dist/index.d.ts`、具体模块声明、锁定源码 `packages/coding-agent/src/` 以及实际导出和执行结果。区分“声明存在”“模块可导入”“扩展可发现”“会话可调用”；只读文档或 grep 到方法名不足以证明兼容。

可从 checkout 只读定位锁定源码：

```bash
nix eval --raw --impure --expr '(builtins.getFlake ("path:" + toString ./.)).inputs.pi.outPath'
```

## 包与依赖

- `modules/agents/providers/pi/package.nix` 在 workspace 构建阶段应用 Pi 补丁，保持 `source` 为普通路径，避免求值时引入 IFD。库与 CLI bundle 均需覆盖；不可只修其中一份编译产物。
- `extensions.nix` 的官方扩展示例与 Pi 宿主共享 `piSource`。第三方包由 `npm-extension.nix` 使用固定 URL、版本与 registry SHA-512 解包，分别在 `extensions.nix`、`subagents.nix`、`goal.nix`、`queue.nix` 声明。升级前复核 tarball 的 package manifest、peer 范围、入口与实际 imports，使用精确匹配的补丁/替换让上游漂移显式失败。
- 现有解包器不执行 npm install。只有无额外 runtime dependencies 的包才能直接沿用；出现新的运行时依赖时，通过可复现 Nix 打包提供并验证解析，不依赖宿主全局 node_modules、网络安装或用户缓存。Pi 提供的 `@earendil-works/pi-*` 与 `typebox` 使用宿主映射，避免打包重复宿主副本。
- 保持 envY 只拥有各个 `envy-*` 扩展链接和声明的 runtime 字段；auth、session、history、队列与目标数据仍由应用管理。skill 注册不扩大软件 package selection。

## 父子 agent 的能力必须分别证明

检查 `subagents.nix`、`files/agents/subagents/Explore.md`、`Plan.md` 以及实际子会话创建代码。不要假定父会话的扩展、MCP 或工具可见性自动继承。

CLI 默认加载内置 codemode；SDK 的 `createAgentSession` 不自动添加它。SDK 使用 `DefaultResourceLoader` 显式注入 `createCodemodeExtension()`，并通过 `defaultTools` 或等效选路启用；仅注册仍可能处于 inactive。`defaultTools = [ "+codemode" ]` 是增量选择，纯工具名列表是替换选择。

分别核对注册集合、active/declaration 集合和 `ctx.tools` / `ctx.executeTool` 的 callable 集合，以及 `exposure`、`codemode.mode`、MCP 的独立过滤边界。`codemode.mode = "only"` 隐藏声明并不等于撤销 callable 能力；`--tools` 也不能独立证明 MCP 不可达。

对 Explore/Plan 等只读子 agent，验证 codemode 只能调用允许的读取/搜索工具，不能借其间接获得 bash、edit、write 或额外 MCP。测试应同时覆盖允许的调用与被禁止的调用，并实际执行子会话路径；父会话工具列表或 prompt 文本不足以证明隔离。

## 隔离 headless 回归

先使用已有离线 harness。确需新增加载/执行验证时：

- 在 `/tmp` 或 `~/.cache/envy` 创建独立 cwd、HOME、agentDir 与 session 路径，用受控环境启动真实宿主，避免 envY provider wrapper 加载真实凭据；所有模型凭据使用占位值。
- 不读取用户的 auth、models、settings、MCP 或项目扩展。只显式加载本次待测资源；SDK 优先使用内存 settings/session 与受控 resource loader，清理 session 和临时目录。
- 使用 `PI_OFFLINE=1` 或 `--offline` 关闭自动网络活动，但它们不是网络沙箱。禁止真实 provider 请求，在导入宿主前设置拒绝网络的测试边界，必要时拦截 HTTP/WebSocket 或使用系统网络隔离；不能只拦截 fetch 就声称所有网络均已隔离。
- print、JSON、RPC 均会加载扩展；测试 `ctx.hasUI`/`ctx.mode` 分支，防止无 UI 崩溃、后台计时器残留或 stdout 混入通知。SDK 路径测试注册、绑定、调用与 dispose；不通过真实 prompt 调用收费模型来证明加载成功。
- JSON 模式不能只看退出码，RPC 的 prompt 接受也不等于完成。需要运行结束证据时核对事件与 `agent_settled`；UI 通知不得污染协议 stdout。

## 选择现有回归

| 变动 | 现有验证入口与覆盖 |
|---|---|
| SSE、重试、thinking 补丁或 Pi 升级 | `package.nix` 的 install checks：`tests/pi-sse-crlf.mjs`、`pi-retry.mjs`，开启 `moreThinking` 时的 `pi-thinking.mjs`；验证库与 CLI bundle |
| 模型列表、思考档位、provider 或 btw | `pi-model-tests`：`tests/models.nix`、`model-lists.nix`、`pi-models.py`、`pi-models.mjs`，覆盖 Nix/运行时配置及真实请求编码器，发送前截获请求 |
| 消息队列扩展 | `pi-queue-tests`：`tests/pi-queue.mjs`，真实宿主发现流程与编辑器的离线回归 |
| goal-x 扩展与补丁 | `pi-goal-tests`：`tests/pi-goal.mjs`，临时目录中的目标读写、任务树、锁与故障回归 |
| 父子 agent、角色或 codemode 能力 | `pi-subagent-tests`：`tests/pi-subagents.mjs`，使用真实宿主与子会话、离线模型及 MCP 替身核对角色、继承、可调用工具和预算边界 |
| wrapper、skill/adapter 选择、manifest | `envy-agent-tests` 与 `platform-option-boundaries`，按实际改动选用；`checks/agent-skills.nix` 由后者求值 |

测试路径均位于 `modules/agents/providers/pi/` 下。先核对当前 `flake.nix` 中的定义；不要把某个扩展的回归通过说成父子 agent 或全部扩展已通过。新增能力没有现有覆盖时，增加针对真实路径的隔离回归，并报告缺口。

例如选择与改动相关的检查：

```bash
nix build --no-link path:.#checks.aarch64-darwin.pi-model-tests
nix build --no-link path:.#checks.aarch64-darwin.pi-queue-tests
nix build --no-link path:.#checks.aarch64-darwin.pi-goal-tests
nix build --no-link path:.#checks.aarch64-darwin.pi-subagent-tests
```

Linux 对应入口为 `checks.x86_64-linux.<name>`，需要 Linux builder 才能本地构建。单纯 `nix flake check path:. --no-build` 只证明求值；CI 在求值后还分别 build 各项。按改动范围选择验证，不强制每次全量构建。

交付时给出改动文件、锁定版本/宿主、实际执行的命令和结果，明确哪些检查仅求值、哪些实际执行，以及剩余平台或能力缺口。验证不自动 apply，不隐式创建 goal 或文档报告，也不 commit/push。
