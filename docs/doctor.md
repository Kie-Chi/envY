# Doctor

envy doctor 检查当前 Darwin/Linux 配置是否可用：config/secrets、安装项、命令、运行状态、应用 state、登录提示和 Darwin TCC。默认不做网络探测。

```bash
envy doctor
envy doctor config
envy doctor system
envy doctor apps --only chrome,codex
envy doctor network
envy doctor --json
```
## 结果与策略

app schema 位于 resources/scripts/envy/schemas/{common,darwin,linux}/apps.py；generic checker 检查 bundle、command、process、state 和 login hint，可靠的应用特有状态才使用 custom checker。doctor 先求值 manifest：被 machine policy exclude 的软件跳过安装/runtime/auth 检查；求值失败才使用保守 fallback。

结果只能显示状态、计数和下一步，不能显示账户名、邮箱、token、API key、cookie 或 secret 原文。

## 扩展

新增 app 优先补 declarative AppSpec：bundles、commands、processes、state_paths、login_hint、permissions、casks/brews/packages。新增 auth 检查放入 focused checker，设置短 timeout；新增 TCC 权限使用 PermissionReq。

macOS TCC 只能检测系统已有记录。相机、麦克风和屏幕录制通常要先由目标 app 实际请求；不要直接修改 TCC 数据库。MDM PPPC 属于系统管理范围。
