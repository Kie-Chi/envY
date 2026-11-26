# 交互习惯与偏好核验

envy habit 管理跨平台交互习惯的 machine policy，也展示已有系统偏好的声明与安全回读结果。可编辑的手势写入选定 host；平台模块负责 native binding、软件依赖和核验元信息。

```bash
envy habit list
envy habit show <id>
envy habit set <id> <value>
envy habit check --json
envy doctor habits --json
envy habit repair
```

list/show 展示声明，不启动 native 回读。set 修改版本化手势政策，显式加 --apply 才立即 apply；repair 本身会 apply。偏好条目用于核验已有设置，不因此新增可写机器选项。

## 声明与所有权

内部 manifest 的 kind 默认为 binding，手势仍须非空。preference 可以没有手势，gesture 为 null。各平台 owning module 贡献 readback，expected 与实际生成配置同源；后端只接受固定 adapter，不执行 manifest 中自造的命令、域名或路径。

- declarative：模块声明配置。声明一致只表示该声明及软件前置条件有效。
- application：应用管理绑定，envY 记录期望但不读取或覆盖应用状态。

iTerm2、Raycast 和 Niri 的两个既有快捷键绑定继续人工验证。配置文件存在或进程运行不足以证明快捷键已注册、目标正确或功能可用。Niri 的最终 text/source 被覆盖、文件停用或目标重定向时，布局适配也转为人工核验，避免沿用原模板的期望。

## 四层证据

habit check 与 Doctor 共用同一比较器。每项 details.verification 分别保留以下证据，各轴包含 status、reason、source 和 scope，整体附 UTC sampledAt 与 declarationFingerprint。

| 证据 | 含义 |
|---|---|
| declaration | statically-consistent 或 invalid；声明、元信息和软件前置条件的静态核对 |
| stored | match、mismatch 或 unknown；受支持偏好存储的定键回读 |
| runtime | match、mismatch 或 unknown；消费者明确报告的当前值 |
| pending | 该项是否有需要重新登录或重启的可靠信号；缺信号为 unknown |

保存值匹配不会升级为运行中一致。当前只有 Niri 布局 IPC 的正向匹配可给出 runtime=match、pending=none；其范围仅为运行中的 compositor 报告了预期布局名称与索引。首批适配没有可靠的重新登录或重启信号，其他 pending 保持 unknown，不能按 generation、启动时间或进程存在推断。

已确认的值漂移、无效声明和缺少必要软件会使 check 失败。无法回读保留 unknown 及原因；人工检查仍可继续。repair 只用声明和软件条件决定是否允许 apply，不因当前值漂移而阻止修复。

## 只读适配范围

| 平台与条目 | 固定读取 | 可证明范围 |
|---|---|---|
| Darwin keyboard-repeat | defaults 的 NSGlobalDomain.KeyRepeat 类型和值 | 保存的整数偏好 |
| Darwin function-key-mode | defaults 的 NSGlobalDomain.com.apple.keyboard.fnState 类型和值 | 保存的布尔偏好 |
| GNOME terminal-scratchpad | custom-tilix-quake 的 GSettings binding | 保存的绑定值 |
| Niri keyboard-layout | niri msg --json keyboard-layouts | 运行中 compositor 的布局名称与当前索引 |

Darwin 期望来自最终 system.defaults，未声明的 null 不贡献条目。GNOME 期望来自最终 dconf binding，定位固定为 org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake/，只读取 binding，不读取 command/name 或其他绑定。Niri 期望来自原有 us 布局的模块契约，不读取运行时 KDL 正文。

工具、schema、会话或权限不足，类型不符、格式未知、超时、输出超限及无法证明的查询上下文分别保留原因。新探针默认 2 秒期限，stdout/stderr 合计最多 8192 bytes，在读取期间限流；另有 0.25 秒回收预算；无法确认清理时丢弃成功值。失败不返回原始 stderr 或环境值。运行态每次重新取样，声明可以复用现有 manifest 缓存；--refresh 更新声明。

核验不写偏好、不启动会话或服务、不触发 Automation、不授予 TCC，也不读取认证、历史、数据库或应用正文。测试使用合成会话、CLI 响应和隔离子进程，不能替代真实桌面验收。

## JSON 与验证

habit.check 与 doctor.habits 使用 schemaVersion=1、command、ok、data、warnings 和失败时的 error，保留旧 machine/platform/failed/results 或 summary/results 顶层字段。输出为单份普通 JSON，不经过终端颜色与自动换行。默认 Doctor 的旧 JSON 形状保持兼容。

checks/habit-readback.nix 已接入两平台的 platform-option-boundaries，覆盖 schema 正反例、最终配置覆盖、null、启用与排除条件，以及自定义 Niri KDL 的人工边界。Python 合成测试覆盖 native 类型、异常与缺失、fresh 回读、所有权、repair gating、JSON 和进程读取限额。真实宿主 apply 与个人偏好回读没有作为本阶段的执行证据。
