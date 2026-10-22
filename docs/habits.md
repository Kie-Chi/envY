# Habit contracts

envy habit 管理跨平台个人交互习惯的 machine policy。期望值写入 host，平台模块将其渲染为 native binding。

```bash
envy habit list
envy habit show <id>
envy habit set <id> <value> --apply
envy habit check
envy habit repair
```
每个 contract 声明 owner：

- declarative：envY 写入并通过 apply 激活；
- external：记录期望值但不覆盖外部管理器，check 只报告状态。

machine block 只保存稳定的 desired values。实现细节、软件 ownership 和平台绑定属于对应 module。set 先改版本化 policy；需要生效时显式使用 --apply。check 发现 malformed、冲突、缺少软件或 binding 漂移时失败。
