# Agent skills 与 Pi 扩展

skill catalog 位于 modules/agents/skills/catalog.nix，由 modules/agents/default.nix 选择，modules/agents/skills.nix 按 adapter 建立发现目录。上游 skill 通过 flake input 锁定，应用时只链接已选择的目录。

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

## Claude Code 插件

envY 只在 `~/.claude/settings.json` 声明 `enabledPlugins`（skill-creator、code-review）；marketplace、插件缓存和 installed_plugins.json 由 Claude Code 自己维护。

```bash
nix flake check path:. --no-build
envy check --all
envy apply
```
