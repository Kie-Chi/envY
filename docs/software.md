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

## Homebrew 指定卸载

Darwin 机器可以在 `hosts/darwin/<id>.nix` 的 managed blocks 外声明需要卸载的 Homebrew 软件：

```nix
envy.darwin.software.homebrew.uninstall.casks = [ "t3-code" ];
envy.darwin.software.homebrew.uninstall.formulae = [ ];
```

`envy apply` 在 Homebrew bundle 前读取已安装清单，仅卸载名单中的已安装项；已经不存在的软件跳过。cask 先于 formula 卸载，formula 批量处理；查询或卸载失败会中止激活。可使用短名称或 `vendor/tap/name`，同一类型的最终安装选择与卸载名单冲突时，求值报错；需要先通过 `exclude` 取消安装选择。

`include`/`exclude` 继续只负责安装选择，不会自动加入卸载名单。全局 cleanup 保持 `none`；指定卸载禁用 autoremove，不使用 zap、force 或 ignore-dependencies，不主动删除应用用户数据，但仍会执行 cask 自带的卸载步骤。声明保存在 manifest 的 `software.homebrew.uninstall` 和行为预览中；代际回滚不会自动重装已卸载的 Homebrew 软件。

## 来源与所有权

来源定义在 envy.software.sources；自定义 host source 在独立 managed block。source registration 只管理 flake input/binding 与 lock；native source 只管理 envY 命名的 repository/keyring 文件。来源 ID、上下文 fingerprint、缓存和安装必须一致。

软件 catalog 不放入 modules/envy；feature module 提供 package，envy 只聚合选择并公开 manifest。

## 安全边界

- 不把 registry 查询结果当作项目锁定 input。
- 不合成未经验证的 canonical URL/reference。
- 不把 secret、token 或明文 secrets 写入 manifest、日志或 Nix eval。
- 来源更新必须事务化：验证全部 machine，失败时同时回滚 source 文件和 flake.lock。

端到端范围见 software-sources-e2e.md。
