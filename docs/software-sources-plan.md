# 软件来源设计

## 对象

1. Source definition：提供者、生态、endpoint、验证方式和支持的 scope。
2. Source binding：把项目声明的 flake input、output 和 source ID 绑定起来。
3. Manifest v3：机器可选择的软件组、stable ID、版本、platform、installer 和 canonical reference。
4. Identity cache：以 source ID 和 context fingerprint 隔离 positive/negative TTL，供 search 与 add 共用。

## 数据与所有权

- envy.software.sources 是版本化 source catalog。
- 自定义 native source 位于 envY managed source block；不接管系统拥有的 APT/DNF/Pacman/Zypper 配置。
- source registration 只写 flake managed input/binding 与 flake.lock；native registration 只写 envY 命名文件。
- machine 文件只保存软件选择和 mirror override，不保存搜索结果的临时 metadata。
- package catalog 由所属 feature 管理，modules/envy 只聚合。

## 解析流程

search 返回带 source ID 的结果。add 必须先从 evaluated manifest、exact identity index 或 provider exact lookup 解析 canonical identity，再写 machine policy。provider unavailable、not found 和验证失败都保持 policy 不变。

source add/rm/sync 以事务运行：修改前保存输入、绑定和 lock，验证所有 Darwin/Linux target，任一步失败整体回滚。

## 各生态

- Nix：使用显式 flake input/output 和 reference metadata。
- Native 包管理器：source 文件由 envY 命名并独立管理，保留外部仓库。
- Homebrew/npm/PyPI/Cargo/Go：source endpoint、版本和安装 backend 分开表达。
- URL artifact：固定 URL 与 hash 必须显式声明；mirror 只能在可信且可验证的范围内选择。

## 验证

```bash
envy sw source ls
envy sw source show <id>
envy sw source search <query>
envy sw source sync <id>
envy check --all
envy sw audit
```
