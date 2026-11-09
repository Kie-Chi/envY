# Doctor

envy doctor 检查当前 Darwin/Linux 配置是否可用：config/secrets、安装项、命令、运行状态、应用 state、登录提示和 Darwin TCC。默认不做网络探测。

```bash
envy doctor
envy doctor config
envy doctor system
envy doctor apps --only chrome,codex
envy doctor agents
envy doctor agents --json
envy doctor network
envy doctor --json
```
## 结果与策略

app schema 位于 resources/scripts/envy/schemas/{common,darwin,linux}/apps.py；generic checker 检查 bundle、command、process、state 和 login hint，可靠的应用特有状态才使用 custom checker。doctor 先求值 manifest：被 machine policy exclude 的软件跳过安装/runtime/auth 检查；求值失败才使用保守 fallback。

结果只能显示状态、计数和下一步，不能显示账户名、邮箱、token、API key、cookie 或 secret 原文。

## Agent 检查

`envy doctor agents` 只读检查 envY 部署的资源：`.config/envy/agent-skills.json` 描述 Claude/Codex 的托管 skill 来源与链接，`.pi/agent/envy-runtime.json` 描述 Pi 的扩展、角色、skills 和 MCP 命令。Claude 与 claude-code 的共享目录按最终目标去重；排除 Pi 时仍检查 Claude/Codex 链接。命令会显示 Pi 即时运行覆盖是否存在；存在覆盖是正常信息，`envy apply` 会清除它。

契约由 Home Manager 从选中的 catalog 和最终 source 生成，只接受只读 Nix store metadata，并比对实际链接及 `SKILL.md` 来源。它不读取完整用户配置或认证，不执行 MCP、不安装或修复插件。Claude 插件的配置声明不作为已安装证据；此检查不验证插件安装状态。

扩展探针使用隔离的临时目录和真实 Pi 宿主，不读取真实 auth、models、sessions，不发起模型请求或启动 MCP。只检查 envY 管理的固定来源；发现声明的最低 Pi 版本时进行比较，未知声明不等于兼容。cron 工厂因 daemon 副作用而跳过并单独报告。加载通过只证明工厂与注册成功，不能代替工具调用、语言服务或 TUI 的集成验证。

尚未部署此版本运行契约时，会提示先 apply；检查本身不 apply、不修复配置，也不输出凭据内容或扩展原始日志。

## 扩展

新增 app 优先补 declarative AppSpec：bundles、commands、processes、state_paths、login_hint、permissions、casks/brews/packages。新增 auth 检查放入 focused checker，设置短 timeout；新增 TCC 权限使用 PermissionReq。

macOS TCC 只能检测系统已有记录。相机、麦克风和屏幕录制通常要先由目标 app 实际请求；不要直接修改 TCC 数据库。MDM PPPC 属于系统管理范围。
