# Agents module

envY 的 modules/agents 管理 agent 安装、provider wrapper、skill catalog 和运行时设置。应用 auth、history、cache、session 与 plugin state 由应用自己管理。

## 结构

- default.nix：只负责组合子模块和平台分发；darwin/、linux/ 只放平台独有实现。
- defaults.nix：共享的 endpoint、credential、agent 实例默认值与 active skills。
- options.nix：`envy.agents`、`envy.agentProfiles` 与 amux 选项。
- runtime-files.nix：把 additionalSettings 与 adapter 声明的 runtime 文件合并进应用自有配置。
- diagnostics.nix：从已选 skills 和最终 Home Manager source 生成 Claude/Codex 的只读链接契约，共享 Claude 目录按目标去重。
- wrapper.nix、prompts.nix：provider 共用的 wrapper 生成、endpoint/credential 解析、别名和 system prompt 拼接；`tests/wrappers.nix` 用假 agent 验证 env 文件加载次数、覆盖顺序和参数顺序。
- providers/：claude.nix、codex.nix、generic.nix 与 pi/。pi/default.nix 生成 wrapper 和 models.json；pi/package.nix 给 Pi 打补丁；pi/extensions.nix 链接 Pi 扩展；pi/subagents.nix 管理 pi-subagents 与角色；pi/diagnostics.nix 生成只含非敏感资源来源的运行契约，供 Agent Doctor 比对实际部署。官方示例来自锁定的 Pi source；第三方 npm 包（goal-x、btw、pi-subagents、pi-queue-steer-factory、pi-powerline）由 pi/npm-extension.nix 按固定版本和 registry sha512 解包，链接为 `~/.pi/agent/extensions/envy-<name>`。
- skills/：default.nix 按 adapter 解析 active skill 并建立 Home Manager discovery links；catalog.nix 是 skill 名称到上游目录的映射；read/、browser/ 是本仓库自带的 skill。
- tools/：default.nix 管理 registry 安装的 agent 工具（codegraph、headroom）；obelisk.nix 从固定 npm tarball 打包会话历史检索 CLI `obelisk`，索引 `~/.obelisk/` 与项目内 `.obelisk/memories/` 归应用和用户所有；web.nix 管理 envy-read、受审查的 MCP 工具、mcp-nixos 与 deepseek-search。deepseek-search 只接入 Pi，复用 `llm-credential-deepseek`，启动时从 sops 文件读取 key，按成功调用计费。
- amux/：容器化 agent 客户端、镜像和测试。
- darwin/：nix-darwin.nix 声明 Codex/ChatGPT cask；pi-cron.nix 链接 cron 扩展，并以 Home Manager LaunchAgent `dev.pi.cron` 托管其 daemon，定时任务通过 envY `pi` wrapper 运行。
- linux/：Codex wrapper（Darwin 用 cask 安装 Codex）、rtk 与桌面机的 Cursor。
- files/agents/：非 Nix 静态文件，包括 provider prompt（prompts/）和 Pi 补丁（patches/）。

## 可写配置预览与冲突保护

`sys.mergeWritable` 继续调用 `envy.structured_config`，仅合并 feature 声明的键及命名空间。独立预览可在开发环境运行 `python -m envy.structured_config --source /path/to/declared.json --target /path/to/settings.json --dry-run --json`；TOML/YAML 目标需要对应的 `--format`，托管键参数与 activation 一致。dry-run 不创建目录、临时文件或修改权限。

预览只列最终变化的键路径及 add/change/remove，以及 create/write/replace_symlink/chmod 操作，不输出旧值、新值、正文或内容摘要。JSON envelope 的 command 为 `structured-config.merge`，并明确提示并发限制。普通 activation 保持成功时静默及原有 bool 返回语义。

准备阶段读取文件与父路径身份，写入前在临时文件 fsync 后复核；内容、身份、权限或链接发生变化时以 conflict 停止并保留当前文件。有效链接读取源内容后替换链接自身，悬空链接、特殊文件或无法解析的配置不会覆盖。幂等返回和仅权限调整也进行检查；私密模式及非托管字段继续保留。

即时 agent 刷新检测到冲突时会保留整个运行态文件事务，不再用旧快照自动回滚；此前已完成的写入可能保留，CLI 以 conflict 失败说明 partial 状态。其他错误继续使用原有回滚流程。

这属于乐观复核，复核与替换之间仍有窗口，无法代替应用配合的原子锁。冲突后应关闭相关应用、重新预览再执行；接口不自动覆盖竞争者写入，也不为应用会话、历史或数据库增加托管范围。

## Codex 配置与凭据

`providers/codex.nix` 把 `envy.agents.codex`（先经 `envy.agentProfiles` 解析）渲染成 Codex 自己的 `~/.codex/config.toml`：托管 `model`、`model_context_window`、`model_max_output_tokens`、`model_provider` 和 `model_providers.envy-<endpoint>`。Codex 只允许 OpenAI 兼容 endpoint 上的 `openai-responses`，未设置 api 时也使用 Responses；Chat Completions 和 Anthropic 在写入前报错。`runtime-files.nix` 按 adapter 聚合所有 codex 实例后只写一次，机器 `additionalSettings` 仍覆盖生成值；`managedKeyPrefixes.model_providers = "envy"` 只替换 envY 命名的 provider，保留 Codex 的 `projects`、`tui`、`plugins` 等键。

Codex 多模型模式由 activation 和即时刷新共用 `envy.agent_codex` 生成 `~/.codex/envy-models.json`，通过 `model_catalog_json` 加载。生成器在临时 `CODEX_HOME` 中运行已安装客户端的 `codex debug models --bundled`，离线复用原生指令和匹配模型的元数据；不调用模型接口，不读取真实认证。模型不在内置目录时沿用原生指令，并使用保守的能力声明。多模型目录在启动时加载，修改后需重启对应客户端；桌面端模型选择器是否展示候选项取决于其内嵌 Codex 版本。

默认选择沿用顶层 profile/model；未指定默认模型时使用列表第一项。多模型的 `contextWindow` 写入各模型元数据，避免全局上下文窗口覆盖切换后的模型；`maxTokens` 保留全局映射，因此候选项必须一致。移除候选项会重写目录，清空列表会解除 envY 生成的目录引用，并清除过期的全局模型限制，保留用户的其他配置。

Codex 从 `~/.codex/auth.json` 读取 API key，而不是 `OPENAI_API_KEY`。activation 的 `mergeCodexAuth` 与 `envy agent refresh` 都会把解密后的 `envy.llm.credentials.<name>` 写入该文件的 `OPENAI_API_KEY`（`auth_mode = "apikey"`）并保留其他 Codex 字段；这是 envY 唯一写入应用 auth 文件的位置，`codex login` 之后需要重新 apply 或 refresh。`envy agent` 用同一套渲染规则即时写 `config.toml` 和 `auth.json`，`tests/test_agent_codex.py` 对比 Nix 与 Python 输出。

## Pi 补丁

pi/package.nix 在 Pi 的 workspace 构建阶段打补丁，库和打包后的 CLI 会一起重新编译。上游求值时会从 `source` 读取 package 与 lock 文件，所以不能给 `source` 套补丁派生，否则会变成 import-from-derivation，导致在另一平台上无法求值那个平台的机器。

- pi-anthropic-sse-crlf.patch：始终启用。Anthropic SSE 的 CRLF 被网络分包拆开时，上游会把 LF 当作空行，提前派发一个没有 data 的事件，报 `Could not parse Anthropic SSE event ...: Unexpected end of JSON input`。补丁让末尾的 CR 等下一个分块到达后再判断。`tests/pi-sse-crlf.mjs` 对库和 CLI bundle 分别验证。
- pi-retry-patterns.patch：始终启用。扩充可自动重试的错误：网关在流中转发的 Bedrock 异常（`InternalServerException`、`ThrottlingException` 等）、无详情或 JSON 被截断的 SSE 错误事件，以及 Anthropic 的 `violative cyber content` 用量政策拦截（多在流中触发，重新生成可能通过，每次重试仍会重新判定）。配额、计费、权限和 Bedrock 内容过滤仍立即失败。重试次数沿用 Pi 默认的 `retry.maxRetries`（3 次）。`tests/pi-retry.mjs` 对库和 CLI bundle 分别验证。
- pi-ultra-thinking.patch：仅在 `envy.agents.pi.moreThinking = true` 时启用，增加 `ultra` 思考等级；由 `tests/pi-thinking.mjs` 验证。

升级 Pi 时，如果补丁不再适用或上游已经修复，构建或安装检查会失败；这时更新或删除对应补丁。

### 模型档位与 btw

`pi/default.nix` 为 OpenAI Completions/Responses 下的 `gpt-6.1-sol` 及其带 `-` 后缀的快照，以及已知网关别名 `stepcode-gpt-6.1-sol`、`stepcode-gpt-6.1-sol-eastus2` 声明合法思考档位：排除 `off`、`minimal`、`ultra`，保留 `low/medium/high`；开启 `moreThinking` 时还保留 `xhigh/max`。显式请求不支持的档位由 Pi 钳制到合法档位。

自定义模型可在 Pi 默认配置、profile 或 `models` 条目中声明 `thinkingLevelMap`，无需继续扩展模型名称匹配。显式映射整体替换兼容默认值；映射中的 `null` 禁用对应档位，整个字段设为 `null` 则清除继承映射并恢复兼容默认值。例如，采用相同 Sol 能力限制的网关模型可声明 `thinkingLevelMap = { off = null; minimal = null; ultra = null; };`。模型 ID 原样用于网关路由，Nix 部署与 Python 即时刷新采用同一语义；其他 adapter 拒绝这项 Pi 专属能力配置。

`/btw` 不继承主会话的思考档位。未指定档位的 Sol 请求省略推理 effort，由服务端采用默认值，避免发送会被拒绝的 `none`；不改扩展的认证、上下文、无工具或会话隔离边界。

`pi-btw-scroll.patch` 在扩展解包时修正弹层的滚动方向和边界。长回答使用 ↑/↓、PageUp/PageDown、Home/End 浏览；全屏模式下，指针位于弹层内时也可用滚轮，抵达边界仍由弹层消费事件。关闭弹层后焦点恢复到编辑器。`pi-coexist-tests` 使用真实 BTW、Goal 和宿主输入分发验证这些边界，其 `.tui` 入口覆盖隔离 CLI/PTY 交互。

`envy agent` 的 Python 运行时生成器使用相同兼容规则，切换 profile 时不会把不支持的档位重新写回 `models.json`。`tests/models.nix` 求值实际 provider 的托管配置；`tests/pi-models.py` 对比 Nix 与运行时配置；`tests/model-lists.nix` 通过实际选项 schema 和共享默认值验证模型列表、profile 覆盖、显式 null、去重及非法输入；`tests/pi-models.mjs` 用真实 `/btw` 执行函数与 Pi 请求编码器执行离线回归，检查模型 schema、档位钳制及两种 OpenAI API 和 Anthropic 的参数。两平台的 `pi-model-tests` flake check 和 CI 运行这些检查；使用假凭据，在请求发送前捕获参数并禁止 fetch，不调用模型。

### Pi 多模型列表

`envy.agents.pi.models` 可以同时注册多个模型，混用 `openai-completions`、`openai-responses` 和 `anthropic-messages`。`envy.agents.codex.models` 也支持同样的 profile 继承、显式覆盖和去重，但所有候选项（含默认选择）必须使用同一个 endpoint、credential 和 `maxTokens`，且 API 必须为 Responses；其他 adapter 不支持非空列表。

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

### Python LSP 诊断与修复预览

`pi/lsp.nix` 固定 `@narumitw/pi-lsp` 0.49.12，并逐个固定 `cross-spawn` 及其传递依赖的 npm SHA-512；通用 npm 解包器不会执行 `npm install`。默认 Python 服务为锁定 nixpkgs 的 `ruff server`，使用绝对 store 路径，覆盖 `.py` 与 `.pyi`。排除 `ruff` 或 `pi` 时，envY 的默认服务列表为空，同时移除这项 Ruff 软件贡献和引用。

`lsp_diagnostics` 返回诊断位置、严重程度和代码；`lsp_fix` 的 `source.fixAll` 默认只预览，返回实际 edits 和修改后的文本，写入需要显式 `write: true`。envY 不创建或合并 `pi-lsp.json`，用户和可信项目的自定义配置仍按上游优先级生效。没有加入 Nix/Rust 服务或额外语言功能。

`pi-lsp-tests` 通过真实 Pi SDK 包装工具启动 Ruff，验证 F401、非空修复预览且源文件不变、正常完成后退出、初始化后取消，以及错误启动参数的负例。测试使用私有 HOME 与配置目录、清空凭据环境并阻断网络，不调用外部模型。两平台都有对应 flake check 与 CI 入口。

### 上下文视图

`pi/views.nix` 固定 `pi-context-view` 0.7.0；实际发布包声明的最低 Pi 版本为 1.1.0，没有额外 npm 运行时依赖。`/context usage` 显示上下文分类，`/context injections` 显示系统提示、工具和注入内容。

打开视图不会自动发起模型探测。已有请求快照时使用捕获的数据；还没有快照时显示估算及明确提示，扩展增量尚未捕获，之后的正常请求会更新快照。配置、快照和会话仍归扩展及 Pi 管理，envY 只拥有扩展链接和禁止自动探测的固定补丁。

`pi-context-tests` 使用真实 SDK 和离线 provider 验证请求计数、捕获与渲染；`nix run path:.#checks.aarch64-darwin.pi-context-tests.tui` 使用私有 HOME 在真实 CLI/PTY 验证视图与编辑器切换。TUI 入口在本机执行，两平台 CI 使用对应 system 的入口，不依赖构建沙箱提供终端。

### 差异视图与共存检查

`pi/views.nix` 固定 `@lyrumu/pi-diff` 0.1.0 和唯一运行时依赖 `diff` 8.0.2，将依赖实体复制到扩展旁，不执行 npm 安装。作者 README 的开发基线是 Pi 1.2；本仓库使用真实 Pi 1.1 SDK 与 CLI/PTY 验证，宿主版本不变。`pi-diff-bounded-scans.patch` 限制自动扫描开销。

`/diff` 查看 Git 差异，`v` 切换布局，Tab 切换范围，Esc 或 q 返回编辑器。检查使用临时 index 基线及实际 before/after 内容，并断言查看前后文件和 index 不变。扩展的历史与缓存仍由应用管理。

消息提交只重置 diff 状态，不读取目录基线；第一次可能修改文件的工具调用前才采集基线。家目录和文件系统根目录不自动全树扫描；其他目录的每次自动扫描以 500 毫秒、10,000 个目录条目和 8 MiB 累计内容为预算，单文件最多 2 MiB。shell 前后、收尾扫描和补丁生成都受预算约束；超限后本轮停止全量扫描，保留明确 `edit/write` 目标的首次已知旧侧，持久记录并在轮次/会话视图提示覆盖不完整。未扫描部分不能视为没有变化，也不能推断为文件删除。手动 Git 范围仍按需使用原生 Git 查询。

`pi-diff-tests` 运行 SDK 及真实 Tracker 回归，覆盖空提交、受保护目录、数量/时间/内容预算、显式文件追踪和 Git 暂存/提交后的旧侧。`.tui` 先复用 context 的真实终端驱动打开两个扩展，再在家目录、非 Git 和 Git 目录验证提交事件与新画面；受控挂起扫描证明用户消息出现前没有扫描，不依靠构造庞大目录来测量。`pi-coexist-tests` 使用 Nix 生成的全部非 cron 扩展来源，验证注册无冲突和真实 codemode → LSP → Ruff F401；其 `.tui` 入口在同一 CLI 加载这些扩展，核对视图、快捷键和编辑器恢复。cron 的 daemon 不在此共存测试中启动；Doctor 明确报告该跳过项。

切换委派模型时使用具体的 `<provider>/<model>` 并核对实际运行报告；本轮改用独立的 Sol 低推理任务，避免模型名称命中默认供应方及重复携带长上下文。

### Pi 会话委托审批

`/auto-assigned on <任务范围>` 由用户显式开启本会话的委托审批；省略范围时使用最近一条用户消息。`/auto-assigned off` 随时关闭，`/auto-assigned status` 查看状态；新会话、重新加载和重启后默认关闭，不自动恢复旧授权。先规划的要求保留，授权范围内可由独立模型审核计划；commit、push、apply 和明确保留给用户的决定不由该开关授权。

`pi/auto-assigned.nix` 打包仓库内扩展，并复用锁定 Pi 的 questionnaire UI 作为人工回退，工具只注册一次。独立审核请求复用当前模型与认证，但不提供工具；普通回复通过 `agent_before_settle` 注入标明来源的代审结果并请求继续，questionnaire 通过同一审核处理四种决定：批准时返回已存在的选项；要求修改时将意见作为代审工具结果交给执行助手，保留委托并继续；已完成时关闭委托，只要求最终报告；交给用户时关闭委托并回退原问卷界面。修改和完成均不伪造选项或打开人工问卷；问卷结果的实际渲染也显示审批模型、决定和理由，不被上游选项渲染器隐藏。需要用户信息、审核失败、超时或取消时停止自动继续。相同提案不重复审批；最多 12 次审核，连续 3 次没有成功 edit/write 时停止。审核只携带开启时所在任务之后的用户、助手、工具结果和代审记录；超过 120,000 字符的审核证据不截断放行，直接交给用户。审核依据、模型和用量保存在会话记录中；questionnaire 的审核用量同时计入工具结果，结束事件中的审核用量单独记录。

`pi-before-settle-abort.patch` 为结束前事件提供独立的取消信号；原宿主低层循环此时已经结束，原来的工具信号无法取消嵌套审核。补丁使 TUI/RPC/SDK 的取消能及时中断审核，并在退出边界时清理。`pi-auto-assigned-tests` 使用真实 SDK、隔离目录和离线模型验证普通计划后继续、问卷四种决定、意见实际进入助手上下文、完成、人工回退、重复、codemode only 嵌套写入计入进展、无进展、无效输出、超时、取消及重新加载；网络请求被拒绝，不使用真实凭据或收费模型。

### Pi footer

`pi/powerline.nix` 固定 pi-powerline 0.11.2 和 registry SHA-512，无额外运行时依赖。打包时将 manifest 入口替换为仓库内的 footer 适配入口，仅导入 `registerFooter`；上游 editor、header、widget 和相应配置命令均不加载，保留 queue 的编辑器。关闭时可在 Pi 设置中将 `powerline` 或 `footer` 设为 false 后重新加载。

适配器补上模型 ID，分支和其余颜色跟随当前 Pi 主题；`system` 是 envY 的默认主题，可跟随终端配色。保留分支、上下文、费用、MCP 和其他扩展状态，读取全局与项目的压缩设置，并把独立委托审核记录中的用量计入累计费用。session start 清理流式状态，退出时释放 footer 订阅和定时器；每处上游源码适配都检查唯一匹配，升级发生漂移即构建失败。

`pi-powerline-tests` 用真实锁定 SDK 和离线模型验证显示、1–200 列宽度、中文、主题切换、会话落盘与恢复、退出清理及 print 模式。`pi-coexist-tests.tui` 使用实际 CLI/PTY，连同 queue、context、diff 等全部部署扩展验证 24/40/120 列、中文输入、dark/light、退出后恢复费用、终端光标恢复，以及非交互输出无 ANSI 污染；模型请求只进入离线替身，不读取真实认证。Linux 检查入口已接入 CI；当前交互证据来自 Darwin。

### Pi 消息队列

`pi/queue.nix` 固定 pi-queue-steer-factory 0.17.8，通过 registry SHA-512 校验解包，链接为 `~/.pi/agent/extensions/envy-queue-steer`。这个版本已有 Pi 1.0 的上游宿主验证，envY 的构建检查使用锁定的 Pi 1.1。普通队列功能不依赖 Pi Fabric。

`pi/extensions.nix` 将 `steeringMode`、`followUpMode` 默认设为 `one-at-a-time`，并通过 writable merge 管理 `keybindings.json` 的三个动作：`Enter` 提交 steer、`Alt+Enter` 添加 follow-up、`Alt+Up` 进入队列编辑。选中行后直接编辑文本，`Alt+Up/Down` 切换行，`Alt+Shift+Up/Down` 重排，`Alt+X` 标记删除，`Alt+Left/Right` 或 `Alt+T` 切换 follow-up/steer，`Enter` 保存、`Escape` 撤销。输入框为空时按 `Enter` 可将队首 follow-up 提升为 steer。macOS 的 Alt 对应 Option。重排、删除和类型切换沿用插件固定按键。

envY 通过 `pi-queue-commands.patch` 给同一可见队列增加 `/queue` 命令，不创建第二个队列控制器。裸 `/queue` 显示状态与用法；`/queue list` 按执行顺序列出行 ID、类型和内容；`/queue add <内容>` 添加 follow-up，`/queue steer <内容>` 添加当前运行的 steer；`/queue edit [行ID]` 打开原有行编辑器，省略 ID 时选择最新入队行；`/queue remove <行ID>` 删除待发送行；`/queue clear` 确认后清空待发送队列，但不终止当前运行；`/queue resume` 恢复原有逐条调度，不把 follow-up 提升为 steer，也不解除单行暂停。已有 `/pause` 与 `/queue-drain` 保持安全暂停和合并发送的语义。

命令空闲入队时保持暂停，使用 `/queue resume` 开始执行；运行中入队沿用原有发送边界，不解除已有暂停。未保存行编辑、执行中的控制操作及尚未完成的安全暂停会阻止冲突命令。清空确认后重新核对队列与会话边界，拒绝过期确认；同步恢复发送失败时保留原行、暂停并显示错误。基础命令同时支持交互 TUI 与 RPC，`/queue edit` 使用交互 TUI；桌面投递计划另通过 RPC 控制请求复用同一编辑会话。RPC 的普通输入继续使用宿主原生队列，不被扩展拦截。RPC 通过 `setStatus("queue", ...)` 上报扩展中 follow-up 的数量、暂停或控制状态及内容摘要，steer 保持独立语义，不计入 Queue 摘要。摘要最多展示 8 行、每行 400 字符，并标记截断及其余条目；`/queue list` 保留完整内容。相同摘要去重，发送、删除、清空及会话切换会更新或移除状态，官方移动客户端可复用既有 Pi 扩展状态入口查看。

`/queue` 的参数候选包含子命令、用法和说明；`edit`、`remove` 的候选来自当前待发送行，显示稳定 ID、类型、暂停标记及内容摘要。输入前缀后按 `Tab` 选择候选，已完整输入的子命令或 ID 不再重复弹出候选，避免 `Enter` 被无意义补全吞掉。未知子命令、缺少参数或无效行 ID 会给出用法提示。

队列、草稿、恢复快照与会话数据归 Pi 和扩展所有。`tests/pi-queue.mjs` 通过真实 Pi 扩展发现流程、宿主模块和编辑器执行离线检查，覆盖逐条发送、编辑、重排、删除、转为 steer、取消、暂停恢复、命令控制、确认竞态、真实补全提供器，以及 RPC 状态摘要、steer 区分、截断、恢复和发送失败；不调用模型。两平台的 `pi-queue-tests` flake check 和 CI 运行这些检查；`pi-coexist-tests.tui` 另通过真实 CLI/PTY 验证子命令和行 ID 补全、入队、行编辑、删除、取消清空及编辑器恢复，所有模型请求进入离线替身。配置通过 `envy apply` 部署后，新启动 Pi 或执行 `/reload` 生效。

### goal-x 扩展

`pi/goal.nix` 固定 goal-x 0.33.0（兼容当前 Pi 1.1），在 npm 包解包后应用 `pi-goal-state-and-stages.patch`，不修改已安装的 store 路径或用户目标。`tests/pi-goal.mjs` 在构建时运行，并通过两平台的 `pi-goal-tests` flake check 暴露；测试只使用临时目录、假会话/UI 和真实扩展读写代码，不调用模型。

- `/goal-clear` 在确认前先持久化缓冲修改，再绕过内存池、文件解析缓存和持久快照读取最新目标；确认后的焦点令牌与文件 revision 都必须仍有效。归档文件写入且 active 文件移除成功，才清空焦点。锁、写入、删除和 revision 冲突会报告具体错误，不再显示假成功。删除失败时可能已留下归档副本，但 active 目标与会话焦点仍保留，便于检查和重试。
- 全局 `~/.pi/agent/pi-goal-x-settings.json` 通过 writable merge 只管理 `hideUnfocusedBanner = true`、`hideUnfocusedPrompt = true`、`autoSelectSingleGoal = false`，保留其他字段。没有焦点的会话不被自动提示阻拦，也不自动接管旧目标。仍可用 `/goal-list`、`/goal-focus`、`/goal-resume` 手动选择。扩展自身的环境变量与项目设置仍按上游优先级覆盖全局默认。
- 普通 goal 与 Sisyphus 都保留完整阶段、顺序、约束和完成条件；不再要求正文只写一句。`create_goal` 支持与正文一起保存经过校验的父子任务树；未显式提供任务树时，从编号、清单及中英文阶段标记中派生任务，单步骤工作不强制拆分。直接创建时仍尊重关闭任务跟踪的设置。引导草拟还校验用户原始阶段数，拒绝阶段更少的摘要提案。
- 界面短标题和 `get_goal` 默认摘要不是完整需求；正文用 `get_goal section=objective`、任务用 `section=tasks` 分页读取。补丁不反向重建以前已被模型压缩的目标，也不能仅凭阶段数判断每个阶段的语义是否完整，仍需核对确认提案。

Goal 紧凑任务列表使用 `Ctrl+Shift+↑/↓` 滚动；envY 将 Pi 全屏主聊天的上一条／下一条导航限定为 `Ctrl+↑/↓`，避免宿主先消费 Goal 按键。只有任务数超过紧凑窗口时才发生列表滚动。键位通过现有 writable merge 部署，重启 Pi 后加载。

### T3 Code（Linux 与 Darwin）

`providers/t3code/` 为两平台提供同一个 Nix nightly 包与通用配置，版本由 `nix-t3code` flake input 锁定。上游 flake 自带所需工具链，不要求跟随 envY 的 nixpkgs。机器通过软件 include/exclude 选择 `t3code-nightly`；Darwin 不再安装 Homebrew 的 `t3-code`。

Goal 的 RPC 摘要与 `goal-tasks` widget 分别去重，任务树通过 provider session 的 `extensionStatuses[].goalTasks` 传递。Queue 同样通过 `queue-messages` widget 发送结构化 follow-up 快照，落在 `extensionStatuses[].queueMessages`，手机继续读取原有文字摘要。快照保留稳定行 ID、顺序、多行正文、队列及单行暂停状态和附件数量，不传输附件数据，也不混入 steer；最多展示 64 条，每条正文 4000 字符，界面明确标记剩余条目和正文截断。`t3code-pi-goal-tasks.patch` 在源码的 patchPhase 应用，buildPhase 同时生成界面、协议和内置后台；构建检查分别核验后台与前端，避免旧后台被新界面掩盖。网页与桌面分别显示 Goal 和紧贴输入框的 Delivery plan，复用 Banner 的正文缩进以对齐标题和边框。投递计划直接复用 Pi 的队首、批次及编辑阻塞计算，区分全队暂停、单行暂停、编辑阻塞、控制操作和下一次派发边界；steer 只影响等待原因，不混入 follow-up 数量和正文。面板默认展示 3 条，支持展开正文及更多条目；行菜单提供编辑、暂停、删除和重排，空输入框的队列编辑快捷键优先操作 Pi 队列。`provider.queue.control` 需 orchestration operate 权限，只访问当前线程的现有 Pi 会话，使用带版本校验的 `/queue-control` 请求及 `queue-control-result` 回执；不创建聊天消息或模型回合。编辑沿用 Pi 的编辑持有语义，保留附件及原输入框草稿；客户端每 30 秒续约，离开视图取消，失联 2 分钟后释放持有。完整编辑正文上限为 65536 字符，失效时保留界面草稿以便重开编辑。仅有文字状态的旧扩展继续显示通用状态栏，旧结构化快照保持只读。移动端保留 Goal 的相应 Sheet 实现。`t3code-pi-queue-messages.patch` 接入 Queue 协议和卡片，构建同时检查两类 widget 的后台标记与界面，并运行 Pi adapter、Goal 和 Queue 列表回归。构建先运行受影响组件的 React Compiler 覆盖检查，再检查六个相关项目的类型；ChatView 依赖编译器稳定渲染输入，新增 ref 必须通过组件边界传递。

桌面和 `t3` CLI 来自同一个 Nix 包。应用自身更新关闭，更新使用 `envy update inputs nix-t3code`，构建验证后运行 `envy apply`。公共模块同时管理稳定的包入口和 `~/.local/bin/t3`，避免旧运行包继续占用命令入口。`packages.<system>.t3code-nightly` 暴露定制包，构建时执行定向协议与任务列表回归；`checks.<system>.t3code-settings` 验证两平台的配置、命令路径、主题和排除策略。

应用继续使用 `~/.t3/userdata`，macOS 浏览器资料继续使用 `~/Library/Application Support/t3code-v2`。配置合并声明的 Agent、外观和桌面客户端模式，不接管认证、历史、缓存和会话文件。

选中 T3 Code 时，envY 使用上游标准的 Darwin `com.t3tools.t3code.service` LaunchAgent 或 Linux `t3code.service` 用户服务运行同一个 Nix 包的 `t3 serve`；桌面关闭内置本地环境，通过配对连接此服务。首次从桌面内置后台切换前，应先退出桌面，避免两个后台争用数据锁与端口；启动服务后用 `t3 pair` 的应用链接完成配对。退出桌面不停止服务。Linux 服务随用户会话启动；注销后运行取决于系统的 linger 设置，envY 不主动修改它。

机器文件中的 `envy.t3code.service.host`、`port`、`lan` 声明 `~/.t3/service.env` 的三个网络字段。激活和服务启动时保留其它变量、多行值和注释，原子合并声明字段，并保持文件权限 `0600`。环境文件按 dotenv 数据解析，不作为 shell 执行；认证和自定义凭据仍由应用管理。默认地址为 `127.0.0.1:3773`；当前 MacBook Air 显式使用 `::`、端口 `3773` 和 `lan = true`，接受 IPv4 与 IPv6。

服务定义声明 `T3CODE_HOME`、Nix 包路径与版本元数据，标准 `t3 service status` 和 `t3 service restart` 操作同一个原生服务。Nix 包或声明的网络配置变化会改变启动脚本与服务定义的路径，Home Manager 在 activation 中重启；声明未变化时保持运行。版本号相同而补丁不同也通过包路径区分。首次迁移会先停用旧 `org.envy.t3code`／`envy-t3code.service`，避免双后台争用数据目录。

安装与修复由 `envy apply` 部署声明式服务；`t3 service install` 对当前 Nix 服务保持幂等，需要修复时提示重新激活 Nix 配置。Nix 托管模式拒绝下载官方 pinned runtime，`t3 update` 和远程自更新也遵循这一边界。服务设置 `settings.json` 支持文件监听重读；`service.env` 与后台程序在进程启动时加载，修改后需要重启，前端资源更新则需要刷新网页或重开桌面。排除 T3 Code 时，envY 不再声明服务，保留用户数据和可写配置。

Darwin 从上游包同一工具链的 Electron 构建完整的 `T3 Code.app`，主程序和辅助进程均在自有应用包内，统一显示为 T3 Code。Finder 入口经过外层 wrapper，继承上游运行路径、资源监控及更新政策；Linux 桌面入口也统一名称。桌面 wrapper 清除后端可能传下来的 `ELECTRON_RUN_AS_NODE`，确保启动图形界面。

在已登录的 macOS 图形会话中，运行 `python3 checks/t3code-desktop.py ~/.local/state/envy/t3code/package` 可检查带该环境变量的隔离启动、系统应用名称、原生进程路径、渲染进程、主窗口及后台就绪。检查使用临时 HOME、T3 数据目录和模拟钥匙串，避免临时 HOME 触发系统默认钥匙串提示；正式启动使用原登录钥匙串。旧版原生手机应用需要升级后才能展示结构化任务树。

### 子 Agent 角色与工具

`pi/subagents.nix` 链接 Explore、Plan、Researcher、NixReviewer 和 Reviewer。角色默认继承调用者模型，调用时可显式覆盖；Researcher 与 NixReviewer 的默认轮数为 12，Reviewer 为 10。Researcher 只声明文件读取、codemode 和 DeepSeek search/fetch；NixReviewer 只声明文件读取、codemode 和 mcp-nixos；Reviewer 不包含 shell、编辑、写入或 MCP 工具。父会话的工具不能视为子会话已有的能力。

`pi-subagent-tests` 使用真实 Pi 宿主和隔离子会话验证角色发现、模型选择、轮数和可调用工具边界，不调用外部模型。修改角色或升级 pi-subagents 时运行这一检查。

### Agent 运行检查与维护 skills

`envy doctor agents` 比对 `.config/envy/agent-skills.json` 中 Claude/Codex 的托管 skills，以及 `.pi/agent/envy-runtime.json` 中 Pi 的扩展、角色、skills 和 MCP 启动命令。两份契约均来自最终声明，不包含密钥或应用状态；排除 Pi 不跳过公共 skill 链接检查。插件配置声明不作为已安装证据，插件安装状态不在此检查范围。即时配置覆盖只显示是否存在及 apply 的清除行为。扩展探针在临时目录调用真实 Pi 的发现流程，禁止网络和子进程，不读取用户认证或会话；cron 工厂会启动 daemon，因此仅报告跳过，不把它记作加载通过。没有明确静态最低版本声明时报告未知兼容性。

`envy-maintain` 按 Nix、Python、Rust 和 JSON 契约改动选择已有检查；`pi-maintain` 指导锁定版本、运行依赖、父子工具和补丁回归。两个 skills 不自动 apply、创建目标或提交。skill 的 source 来自锁定 flake input 或仓库内目录；catalog 校验每个选中 source 都有 SKILL.md，并检查 target 兼容性。claude-code 与 claude 共用发现目录。

### Skill 行为案例

`pi/tests/skill-cases.json` 固定四项只读案例：envY Python 验证选路、Pi 扩展维护、diff 审查，以及不应加载 skill 的普通问答。每项记录 prompt、应读取与不应读取的 skill，以及禁止动作。`checks/agent-skills.nix` 复用 catalog 校验引用及 Pi 可发现性；离线角色测试继续验证工具边界。这些静态与模拟检查不证明真实模型遵循指令。

需要真实行为评估时，通过现有 subagent 工具分别派发各项 prompt，使用独立上下文、明确的 `<provider>/<model>`、low、`max_turns = 4`；保持题目中的只读限制，不给 agent 期望答案。核对实际工具调用、`SKILL.md` 读取结果及最终答复，记录未命中、禁止调用、实际模型、耗时与 usage。普通问答应无工具调用。不能只凭模型自报已使用 skill 判定通过。

真实模型评估使用供应方额度，不在默认 CI 中调用；案例和离线加载测试分开报告。本轮评估的会话收据放在临时证据目录，不把用户会话或模型输出纳入声明式配置。

`envy-github-actions` 使用锁定 nixpkgs 的 zizmor，隔离 HOME/缓存并以 `--offline --no-config --strict-collection` 审计 workflow；不使用凭据或 `--fix`。两平台有 flake check，CI 构建对应入口。Action 使用现有版本对应的完整 SHA，checkout 关闭凭据持久化。

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
