# Agents module

envY 的 modules/agents 管理 agent 安装、provider wrapper、skill catalog 和运行时设置。应用 auth、history、cache、session 与 plugin state 由应用自己管理。

## 结构

- default.nix：默认 agent、endpoint、credential 和 active skills。
- skills/catalog.nix：skill 名称到上游目录的映射。
- skills.nix：按 adapter 解析 active skill，建立 Home Manager discovery links。
- prompts/：provider prompt。
- web-tools.nix：受审查的 MCP 工具与 mcp-nixos。

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
