# Agents module

envY 的 modules/agents 管理 agent 安装、provider wrapper、skill catalog 和运行时设置。应用 auth、history、cache、session 与 plugin state 由应用自己管理。

## 结构

- default.nix：只负责组合子模块和平台分发；darwin/、linux/ 只放平台独有实现。
- defaults.nix：共享的 endpoint、credential、agent 实例默认值与 active skills。
- options.nix：`envy.agents`、`envy.agentProfiles` 与 amux 选项。
- runtime-files.nix：把 additionalSettings 与 adapter 声明的 runtime 文件合并进应用自有配置。
- wrapper.nix、prompts.nix：provider 共用的 wrapper 生成、endpoint/credential 解析、别名和 system prompt 拼接；`tests/wrappers.nix` 用假 agent 验证 env 文件加载次数、覆盖顺序和参数顺序。
- providers/：claude.nix、codex.nix、generic.nix 与 pi/。pi/default.nix 生成 wrapper 和 models.json；pi/package.nix 给 Pi 打补丁；pi/extensions.nix 链接 Pi 扩展。官方示例来自锁定的 Pi source；第三方 npm 包（goal-x、btw）由 pi/npm-extension.nix 按固定版本和 registry sha512 解包，链接为 `~/.pi/agent/extensions/envy-<name>`。
- skills/：default.nix 按 adapter 解析 active skill 并建立 Home Manager discovery links；catalog.nix 是 skill 名称到上游目录的映射；read/、browser/ 是本仓库自带的 skill。
- tools/：default.nix 管理 registry 安装的 agent 工具（codegraph、headroom）；web.nix 管理 envy-read、受审查的 MCP 工具与 mcp-nixos。
- amux/：容器化 agent 客户端、镜像和测试。
- darwin/：nix-darwin.nix 声明 Codex/ChatGPT cask；pi-cron.nix 链接 cron 扩展，并以 Home Manager LaunchAgent `dev.pi.cron` 托管其 daemon，定时任务通过 envY `pi` wrapper 运行。
- linux/：Codex wrapper（Darwin 用 cask 安装 Codex）、rtk 与桌面机的 Cursor。
- files/agents/：非 Nix 静态文件，包括 provider prompt（prompts/）和 Pi 补丁（patches/）。

## Pi 补丁

pi/package.nix 在 Pi 的 workspace 构建阶段打补丁，库和打包后的 CLI 会一起重新编译。上游求值时会从 `source` 读取 package 与 lock 文件，所以不能给 `source` 套补丁派生，否则会变成 import-from-derivation，导致在另一平台上无法求值那个平台的机器。

- pi-anthropic-sse-crlf.patch：始终启用。Anthropic SSE 的 CRLF 被网络分包拆开时，上游会把 LF 当作空行，提前派发一个没有 data 的事件，报 `Could not parse Anthropic SSE event ...: Unexpected end of JSON input`。补丁让末尾的 CR 等下一个分块到达后再判断。`tests/pi-sse-crlf.mjs` 对库和 CLI bundle 分别验证。
- pi-ultra-thinking.patch：仅在 `envy.agents.pi.moreThinking = true` 时启用，增加 `ultra` 思考等级；由 `tests/pi-thinking.mjs` 验证。

升级 Pi 时，如果补丁不再适用或上游已经修复，构建或安装检查会失败；这时更新或删除对应补丁。

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
