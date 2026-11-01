# Agents module

envY 的 modules/agents 管理 agent 安装、provider wrapper、skill catalog 和运行时设置。应用 auth、history、cache、session 与 plugin state 由应用自己管理。

## 结构

- default.nix：只负责组合子模块和平台分发；darwin/、linux/ 只放平台独有实现。
- defaults.nix：共享的 endpoint、credential、agent 实例默认值与 active skills。
- options.nix：`envy.agents`、`envy.agentProfiles` 与 amux 选项。
- runtime-files.nix：把 additionalSettings 与 adapter 声明的 runtime 文件合并进应用自有配置。
- wrapper.nix、prompts.nix：provider 共用的 wrapper 生成、endpoint/credential 解析、别名和 system prompt 拼接；`tests/wrappers.nix` 用假 agent 验证 env 文件加载次数、覆盖顺序和参数顺序。
- providers/：claude.nix、codex.nix、generic.nix 与 pi/。pi/default.nix 生成 wrapper 和 models.json；pi/package.nix 给 Pi 打补丁；pi/extensions.nix 链接 Pi 扩展；pi/subagents.nix 管理 pi-subagents 与内置 agent 覆盖。官方示例来自锁定的 Pi source；第三方 npm 包（goal-x、btw、pi-subagents）由 pi/npm-extension.nix 按固定版本和 registry sha512 解包，链接为 `~/.pi/agent/extensions/envy-<name>`。
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

### goal-x 扩展

`pi/goal.nix` 固定 goal-x 0.33.0（兼容当前 Pi 1.1），在 npm 包解包后应用 `pi-goal-state-and-stages.patch`，不修改已安装的 store 路径或用户目标。`tests/pi-goal.mjs` 在构建时运行，并通过两平台的 `pi-goal-tests` flake check 暴露；测试只使用临时目录、假会话/UI 和真实扩展读写代码，不调用模型。

- `/goal-clear` 在确认前先持久化缓冲修改，再绕过内存池、文件解析缓存和持久快照读取最新目标；确认后的焦点令牌与文件 revision 都必须仍有效。归档文件写入且 active 文件移除成功，才清空焦点。锁、写入、删除和 revision 冲突会报告具体错误，不再显示假成功。删除失败时可能已留下归档副本，但 active 目标与会话焦点仍保留，便于检查和重试。
- 全局 `~/.pi/agent/pi-goal-x-settings.json` 通过 writable merge 只管理 `hideUnfocusedBanner = true`、`hideUnfocusedPrompt = true`、`autoSelectSingleGoal = false`，保留其他字段。没有焦点的会话不被自动提示阻拦，也不自动接管旧目标。仍可用 `/goal-list`、`/goal-focus`、`/goal-resume` 手动选择。扩展自身的环境变量与项目设置仍按上游优先级覆盖全局默认。
- 普通 goal 与 Sisyphus 都保留完整阶段、顺序、约束和完成条件；不再要求正文只写一句。`create_goal` 支持与正文一起保存经过校验的父子任务树；未显式提供任务树时，从编号、清单及中英文阶段标记中派生任务，单步骤工作不强制拆分。直接创建时仍尊重关闭任务跟踪的设置。引导草拟还校验用户原始阶段数，拒绝阶段更少的摘要提案。
- 界面短标题和 `get_goal` 默认摘要不是完整需求；正文用 `get_goal section=objective`、任务用 `section=tasks` 分页读取。补丁不反向重建以前已被模型压缩的目标，也不能仅凭阶段数判断每个阶段的语义是否完整，仍需核对确认提案。

skill 的 source 来自锁定 flake input；catalog 校验每个选中 source 都有 SKILL.md，并检查 target 兼容性。claude-code 与 claude 共用发现目录。

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
