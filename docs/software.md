# 软件 policy 与来源

envy software（别名 envy sw）是软件 policy 的边界。机器 manifest 表达选择，安装 backend 执行选择，source catalog 负责发现和验证。

## 命令

```bash
envy sw ls --details
envy sw add/rm <item>
envy sw en/dis <group> <id>
envy sw search <query>
envy sw source ls/show/search/add/rm/sync
envy sw audit
envy sw why <item>
envy sw cache status/clean
```
add、rm 会预览 desired state；en/dis 只改稳定 ID 的启用或排除。--clean 只能整理目标 ID 的 envY 条目。

## Manifest

group ID 使用 <ecosystem>.<scope>.<kind>；platform 和 installer 是字段。Nix package 必须提供以最终 lib.getName 为 key 的 reference：nix:<attr-path>、flake:<input>#<attr> 或 local:<path>。

source identity 必须由 evaluated manifest、精确 identity index 或 provider exact lookup 证明。找不到、provider 不可用或 reference 未验证时，命令保持 machine policy 不变。

## 来源与所有权

来源定义在 envy.software.sources；自定义 host source 在独立 managed block。source registration 只管理 flake input/binding 与 lock；native source 只管理 envY 命名的 repository/keyring 文件。来源 ID、上下文 fingerprint、缓存和安装必须一致。

软件 catalog 不放入 modules/envy；feature module 提供 package，envy 只聚合选择并公开 manifest。

## 安全边界

- 不把 registry 查询结果当作项目锁定 input。
- 不合成未经验证的 canonical URL/reference。
- 不把 secret、token 或明文 secrets 写入 manifest、日志或 Nix eval。
- 来源更新必须事务化：验证全部 machine，失败时同时回滚 source 文件和 flake.lock。

端到端范围见 software-sources-e2e.md。
