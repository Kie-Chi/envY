# Agents module

envY 的 modules/agents 管理 agent 安装、provider wrapper、skill catalog 和运行时设置。应用 auth、history、cache、session 与 plugin state 由应用自己管理。

## 结构

- default.nix：只负责组合子模块和平台分发；darwin/、linux/ 只放平台独有实现。
- defaults.nix：共享的 endpoint、credential、agent 实例默认值与 active skills。
- options.nix：`envy.agents`、`envy.agentProfiles` 与 amux 选项。
- runtime-files.nix：把 additionalSettings 与 adapter 声明的 runtime 文件合并进应用自有配置。
- wrapper.nix、prompts.nix：provider 共用的 wrapper 生成、endpoint/credential 解析、别名和 system prompt 拼接；`tests/wrappers.nix` 用假 agent 验证 env 文件加载次数、覆盖顺序和参数顺序。
- providers/：claude.nix、codex.nix、generic.nix 与 pi/。pi/default.nix 生成 wrapper 和 models.json；pi/package.nix 给 Pi 打补丁；pi/extensions.nix 链接 Pi 扩展；pi/subagents.nix 管理 pi-subagents 与角色；pi/diagnostics.nix 生成只含非敏感资源来源的运行契约，供 Agent Doctor 比对实际部署。官方示例来自锁定的 Pi source；第三方 npm 包（goal-x、btw、pi-subagents、pi-queue-steer-factory）由 pi/npm-extension.nix 按固定版本和 registry sha512 解包，链接为 `~/.pi/agent/extensions/envy-<name>`。
- skills/：default.nix 按 adapter 解析 active skill 并建立 Home Manager discovery links；catalog.nix 是 skill 名称到上游目录的映射；read/、browser/ 是本仓库自带的 skill。
- tools/：default.nix 管理 registry 安装的 agent 工具（codegraph、headroom）；obelisk.nix 从固定 npm tarball 打包会话历史检索 CLI `obelisk`，索引 `~/.obelisk/` 与项目内 `.obelisk/memories/` 归应用和用户所有；web.nix 管理 envy-read、受审查的 MCP 工具、mcp-nixos 与 deepseek-search。deepseek-search 只接入 Pi，复用 `llm-credential-deepseek`，启动时从 sops 文件读取 key，按成功调用计费。
- amux/：容器化 agent 客户端、镜像和测试。
- darwin/：nix-darwin.nix 声明 Codex/ChatGPT cask；pi-cron.nix 链接 cron 扩展，并以 Home Manager LaunchAgent `dev.pi.cron` 托管其 daemon，定时任务通过 envY `pi` wrapper 运行。
- linux/：Codex wrapper（Darwin 用 cask 安装 Codex）、rtk 与桌面机的 Cursor。
- files/agents/：非 Nix 静态文件，包括 provider prompt（prompts/）和 Pi 补丁（patches/）。

## Pi 补丁

pi/package.nix 在 Pi 的 workspace 构建阶段打补丁，库和打包后的 CLI 会一起重新编译。上游求值时会从 `source` 读取 package 与 lock 文件，所以不能给 `source` 套补丁派生，否则会变成 import-from-derivation，导致在另一平台上无法求值那个平台的机器。

- pi-anthropic-sse-crlf.patch：始终启用。Anthropic SSE 的 CRLF 被网络分包拆开时，上游会把 LF 当作空行，提前派发一个没有 data 的事件，报 `Could not parse Anthropic SSE event ...: Unexpected end of JSON input`。补丁让末尾的 CR 等下一个分块到达后再判断。`tests/pi-sse-crlf.mjs` 对库和 CLI bundle 分别验证。
- pi-retry-patterns.patch：始终启用。扩充可自动重试的错误：网关在流中转发的 Bedrock 异常（`InternalServerException`、`ThrottlingException` 等）、无详情或 JSON 被截断的 SSE 错误事件，以及 Anthropic 的 `violative cyber content` 用量政策拦截（多在流中触发，重新生成可能通过，每次重试仍会重新判定）。配额、计费、权限和 Bedrock 内容过滤仍立即失败。重试次数沿用 Pi 默认的 `retry.maxRetries`（3 次）。`tests/pi-retry.mjs` 对库和 CLI bundle 分别验证。
- pi-ultra-thinking.patch：仅在 `envy.agents.pi.moreThinking = true` 时启用，增加 `ultra` 思考等级；由 `tests/pi-thinking.mjs` 验证。

升级 Pi 时，如果补丁不再适用或上游已经修复，构建或安装检查会失败；这时更新或删除对应补丁。

### 模型档位与 btw

`pi/default.nix` 为 OpenAI Completions/Responses 下的 `gpt-6.1-sol` 及其带 `-` 后缀的别名、快照声明合法思考档位：排除 `off`、`minimal`、`ultra`，保留 `low/medium/high`；开启 `moreThinking` 时还保留 `xhigh/max`。显式请求不支持的档位由 Pi 钳制到合法档位，其他模型和 Anthropic 兼容设置保持原样。

`/btw` 不继承主会话的思考档位。未指定档位的 Sol 请求省略推理 effort，由服务端采用默认值，避免发送会被拒绝的 `none`；不改扩展的认证、上下文、无工具或会话隔离边界。

`envy agent` 的 Python 运行时生成器使用相同兼容规则，切换 profile 时不会把不支持的档位重新写回 `models.json`。`tests/models.nix` 求值实际 provider 的托管配置；`tests/pi-models.py` 对比 Nix 与运行时配置；`tests/model-lists.nix` 通过实际选项 schema 和共享默认值验证模型列表、profile 覆盖、显式 null、去重及非法输入；`tests/pi-models.mjs` 用真实 `/btw` 执行函数与 Pi 请求编码器执行离线回归，检查模型 schema、档位钳制及两种 OpenAI API 和 Anthropic 的参数。两平台的 `pi-model-tests` flake check 和 CI 运行这些检查；使用假凭据，在请求发送前捕获参数并禁止 fetch，不调用模型。

### Pi 多模型列表

`envy.agents.pi.models` 可以同时注册多个模型，混用 `openai-completions`、`openai-responses` 和 `anthropic-messages`。只有 Pi 实例支持非空列表，其他 adapter 的配置不变。

```nix
envy.agents.pi.models = [
  { profile = "stepfun-openai-new"; }
  { profile = "stepfun-openai-new"; model = "deepseek-v4.1-flash-qiye"; }
  { profile = "hb-sub2api"; }
  {
    model = "deepseek-v4-pro";
    endpoint = "deepseek-openai";
    credential = "deepseek";
    api = "openai-completions";
  }
];
```

示例中的 profile 和连接名称需先在所选机器中定义。`envy.agents.pi.contextWindow`、`maxTokens` 是默认模型与所有候选模型的公共参数，覆盖顺序为 Pi 级公共参数 → 当前引用的 profile 非空参数 → 列表条目显式参数；profile 中未填写或为 `null` 的模型参数保留 Pi 级值。endpoint、credential、api 和 model 不在候选之间传播，仍由各自 profile 或条目决定。显式 `null` 清除继承值：`api = null` 恢复按最终 endpoint 协议推导，`credential = null` 使用无认证占位 key，不借用其他连接的凭据，模型参数设为 `null` 则不输出该参数。最终 model 和 endpoint 必须有效。

保留列表外的 `profile` 或单模型字段时，原选择仍为默认模型并自动加入候选；列表外的 profile 保持原有优先级，不采用列表项的覆盖语义。只配置列表时，第一项为默认模型。空列表回到旧单模型方式；重复的最终配置去重，同一连接、同一模型的冲突参数会报错。

多模型模式按 endpoint、最终 API、credential 引用生成稳定的 provider 标识；调整顺序、切换默认 profile 不改变已有连接标识。每个连接从 sops 模板或私有运行时环境读取独立的 key，`models.json` 不保存密钥。首次切换到列表模式会使用新的 provider 标识；单模型模式继续使用 `envy-pi`。

`envy agent set envy.agents.pi.models '<JSON array>'` 会校验、保存并即时刷新运行时配置，`envy agent profile use pi <profile>` 只更新默认选择，不丢弃候选列表或改写 Pi 级公共模型参数；编辑 Pi 级 `contextWindow`、`maxTokens` 也保留默认 profile 引用。两者都对新启动的 Pi 生效。直接编辑机器 Nix 文件或使用 `envy config set` 后，通过 `envy apply` 部署。之后日常用 Pi 的 `/model`、`/scoped-models` 或 `Ctrl+P` 切换已注册的模型，不再需要改配置。

部署和即时刷新只替换 `models.json` 中的 `envy-pi` / `envy-pi-*` 命名空间，清理退出列表的托管 provider，保留其他 provider 和用户字段。apply 会清除 envY 自己的 Pi 即时覆盖文件，使机器配置重新成为生效来源，不修改 Pi 的 auth、会话或历史。

### Pi 消息队列

`pi/queue.nix` 固定 pi-queue-steer-factory 0.17.8，通过 registry SHA-512 校验解包，链接为 `~/.pi/agent/extensions/envy-queue-steer`。这个版本已有 Pi 1.0 的上游宿主验证，envY 的构建检查使用锁定的 Pi 1.1。普通队列功能不依赖 Pi Fabric。

`pi/extensions.nix` 将 `steeringMode`、`followUpMode` 默认设为 `one-at-a-time`，并通过 writable merge 管理 `keybindings.json` 的三个动作：`Enter` 提交 steer、`Alt+Enter` 添加 follow-up、`Alt+Up` 进入队列编辑。选中行后直接编辑文本，`Alt+Up/Down` 切换行，`Alt+Shift+Up/Down` 重排，`Alt+X` 标记删除，`Alt+Left/Right` 或 `Alt+T` 切换 follow-up/steer，`Enter` 保存、`Escape` 撤销。输入框为空时按 `Enter` 可将队首 follow-up 提升为 steer。macOS 的 Alt 对应 Option。重排、删除和类型切换沿用插件固定按键。

队列、草稿、恢复快照与会话数据归 Pi 和扩展所有。`tests/pi-queue.mjs` 通过真实 Pi 扩展发现流程、宿主模块和编辑器执行离线检查，覆盖逐条发送、编辑、重排、删除、转为 steer、取消与暂停恢复；不调用模型。两平台的 `pi-queue-tests` flake check 和 CI 运行这些检查。配置通过 `envy apply` 部署后，新启动 Pi 或执行 `/reload` 生效。

### goal-x 扩展

`pi/goal.nix` 固定 goal-x 0.33.0（兼容当前 Pi 1.1），在 npm 包解包后应用 `pi-goal-state-and-stages.patch`，不修改已安装的 store 路径或用户目标。`tests/pi-goal.mjs` 在构建时运行，并通过两平台的 `pi-goal-tests` flake check 暴露；测试只使用临时目录、假会话/UI 和真实扩展读写代码，不调用模型。

- `/goal-clear` 在确认前先持久化缓冲修改，再绕过内存池、文件解析缓存和持久快照读取最新目标；确认后的焦点令牌与文件 revision 都必须仍有效。归档文件写入且 active 文件移除成功，才清空焦点。锁、写入、删除和 revision 冲突会报告具体错误，不再显示假成功。删除失败时可能已留下归档副本，但 active 目标与会话焦点仍保留，便于检查和重试。
- 全局 `~/.pi/agent/pi-goal-x-settings.json` 通过 writable merge 只管理 `hideUnfocusedBanner = true`、`hideUnfocusedPrompt = true`、`autoSelectSingleGoal = false`，保留其他字段。没有焦点的会话不被自动提示阻拦，也不自动接管旧目标。仍可用 `/goal-list`、`/goal-focus`、`/goal-resume` 手动选择。扩展自身的环境变量与项目设置仍按上游优先级覆盖全局默认。
- 普通 goal 与 Sisyphus 都保留完整阶段、顺序、约束和完成条件；不再要求正文只写一句。`create_goal` 支持与正文一起保存经过校验的父子任务树；未显式提供任务树时，从编号、清单及中英文阶段标记中派生任务，单步骤工作不强制拆分。直接创建时仍尊重关闭任务跟踪的设置。引导草拟还校验用户原始阶段数，拒绝阶段更少的摘要提案。
- 界面短标题和 `get_goal` 默认摘要不是完整需求；正文用 `get_goal section=objective`、任务用 `section=tasks` 分页读取。补丁不反向重建以前已被模型压缩的目标，也不能仅凭阶段数判断每个阶段的语义是否完整，仍需核对确认提案。

### 子 Agent 角色与工具

`pi/subagents.nix` 链接 Explore、Plan、Researcher、NixReviewer 和 Reviewer。角色默认继承调用者模型，调用时可显式覆盖；Researcher 与 NixReviewer 的默认轮数为 12，Reviewer 为 10。Researcher 只声明文件读取、codemode 和 DeepSeek search/fetch；NixReviewer 只声明文件读取、codemode 和 mcp-nixos；Reviewer 不包含 shell、编辑、写入或 MCP 工具。父会话的工具不能视为子会话已有的能力。

`pi-subagent-tests` 使用真实 Pi 宿主和隔离子会话验证角色发现、模型选择、轮数和可调用工具边界，不调用外部模型。修改角色或升级 pi-subagents 时运行这一检查。

### Agent 运行检查与维护 skills

`envy doctor agents` 比对 `.pi/agent/envy-runtime.json` 中 envY 管理的扩展、角色、skills 和 MCP 启动命令。契约不包含密钥或应用状态；即时配置覆盖只显示是否存在及 apply 的清除行为。扩展探针在临时目录调用真实 Pi 的发现流程，禁止网络和子进程，不读取用户认证或会话；cron 工厂会启动 daemon，因此仅报告跳过，不把它记作加载通过。没有明确静态最低版本声明时报告未知兼容性。

`envy-maintain` 按 Nix、Python、Rust 和 JSON 契约改动选择已有检查；`pi-maintain` 指导锁定版本、运行依赖、父子工具和补丁回归。两个 skills 不自动 apply、创建目标或提交。skill 的 source 来自锁定 flake input 或仓库内目录；catalog 校验每个选中 source 都有 SKILL.md，并检查 target 兼容性。claude-code 与 claude 共用发现目录。

## 配置边界

machine 文件只提供非敏感 endpoint、model、adapter 和 skill selection。API key 通过 sops credential 引用，不能进入 Nix 字符串或 agent 配置明文。provider wrapper 可以生成稳定命令，但不管理应用-owned 数据。

新增 agent/provider 时：

1. 在对应 module 声明 adapter、package、endpoint 和 credential 引用。
2. 将敏感值接入 feature 自己的 sops/template 声明。
3. 需要 skill 时在 catalog 注册上游 input，并限制 targets。
4. 用 envy check --all 验证 Darwin/Linux。

远程 agent relay 的连接参数属于 provider wrapper；不要把 host key、token 或会话状态写入仓库。

```bash
nix flake check path:. --no-build
envy doctor
envy apply
```
