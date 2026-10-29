# Agent skills 与 Pi 扩展

skill catalog 位于 modules/agents/skills/catalog.nix，由 modules/agents/defaults.nix 选择，modules/agents/skills/default.nix 按 adapter 建立发现目录。上游 skill 通过 flake input 锁定，应用时只链接已选择的目录。

## 当前 skill

- anti-defensive-writing：直接表达结论，删除无信息的免责声明和重复限制。
- read、browser：网页/PDF 读取与隔离浏览器。
- academic research、presentation、animation、React/Vercel 等 skill：按 catalog 与 active 列表分发。
- health、hunt、check：来自 waza，分别用于 agent 配置体检、先定位根因再修复、审查 diff 与发布前检查。
- prompt-master：只在明确要求编写或改进 prompt 时触发。

添加 skill：增加 flake input（flake = false）、catalog source/targets 和 active 名称，确认上游包含 SKILL.md，再运行全机器求值。不要把应用 auth、history、cache 或 session 文件纳入声明式配置。

## Pi

Pi 使用 codemode = only；未知 MCP 工具需按需发现。browser 登录使用隔离 profile ~/.local/share/envy/browser/profile，不读取或复制日常浏览器 cookies，使用后关闭。

MCP 工具和 skill 通过项目的 source、secret、mirror 和 Nix 配置分发。

## Pi 扩展

envY 只管理扩展代码和 cron daemon 的 LaunchAgent。目标、任务、运行日志与 daemon 状态由扩展自己保存：goal-x 写入项目内 `.pi/goals/`（已加入全局 git ignore），cron 写入 `~/.pi/agent/cron/`。不要用 `pi install` 再安装同名包，否则会重复注册 `/goal`、`/cron` 或 `/btw`。

升级 npm 扩展时，先确认新版本的 Pi peer 范围包含锁定的 Pi 版本，并确认没有运行时 dependencies，再同时更新版本、URL 和 registry integrity。

官方示例 notify 与 subagent 在构建时做最小改写：notify 只在终端交互模式发送 OSC 通知；subagent 通过 envY `pi` wrapper 启动子进程，示例 agent 不固定模型而继承调用方模型。agent 链接为 `~/.pi/agent/agents/envy-<name>.md`，工作流模板提供 `/implement`、`/scout-and-plan`、`/implement-and-review`。

## Claude Code 插件

envY 只在 `~/.claude/settings.json` 声明 `enabledPlugins`（skill-creator、code-review）；marketplace、插件缓存和 installed_plugins.json 由 Claude Code 自己维护。

```bash
nix flake check path:. --no-build
envy check --all
envy apply
```
