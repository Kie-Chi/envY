# 机器配置

每台设备对应一个 host module，所有非敏感机器值都写在 hosts/<platform>/<id>.nix。仓库使用共享 master，机器差异不进入长期分支。

## 文件

```bash
.device-label                 # ignored: machine_id, sops_label
hosts/default.nix             # 可选共享默认值
hosts/darwin/<id>.nix         # Darwin machine module
hosts/linux/<id>.nix          # Linux machine module

envy host init <id>            # import 或 copy default
envy host list
envy host status
envy host select <id>
envy host diff <a> <b>
envy host matrix
envy host check [id]
```
import 保持对 hosts/default.nix 的继承；copy 保存当前默认值快照。host init 不选择软件。

## 选项边界

公共值使用 envy.user.*、envy.git.*、envy.llm.*、envy.vscode.* 和 envy.software.*。只有 Darwin proxy/Homebrew 等专属值放入 envy.darwin.*；Linux 不得获得 proxy schema。

模块通过 package/cask/brew 选择软件。软件 include/exclude 的最终聚合在 modules/envy，具体 package 和 derivation 由所属 feature 提供。

## 受管区块

setup 和 config refine 只更新 host 文件中的 ENVY MANAGED CONFIG；软件命令只更新其受管 include/exclude 区域。手工配置和外部 exclusions 必须保留。

## 镜像

profile 和生态覆盖属于 machine policy。envy mirror set/reset 只修改 ENVY MANAGED MIRROR OVERRIDES；长期实现由 modules/mirrors 消费。bootstrap 环境、Darwin Homebrew、Linux APT 和 Flake source mirror 的职责见 mirrors.md。
