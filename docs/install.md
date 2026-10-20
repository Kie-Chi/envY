# 安装与 bootstrap

## 入口

- install.sh 在仓库外运行：取得或复用 checkout，然后交给 setup.sh。
- setup.sh 安装 Nix，运行最小 setup flake app，再执行 setup.py。
- setup.py 负责 machine、非敏感配置、软件 exclusions 和 sops secrets。

```bash
bash install.sh
bash setup.sh
```
远程 bootstrap 可用 --mirror china|upstream、固定 release/tag、指定 clone 目录；参数以 bash install.sh --help 为准。已有 checkout 会复用，不会自动创建软件选择或覆盖手工配置。

## 镜像边界

bootstrap 的临时环境由 resources/scripts/mirror-env.sh 和 nix-trust.sh 设置；它只服务于第一次获取仓库和 setup runtime。setup 写入机器配置后，长期环境由 modules/mirrors 和 apply 管理。端点必须来自 modules/mirrors/catalog.nix。

不要把 token 放进 URL；不要自动改系统 APT source、运行 chsrc set 或把不可信代理用于固定 hash 下载。Flake source mirror 只对带 lock hash 的受支持 source 做有校验的替换。

## 初始化机器

```bash
envy host init <id>       # import 或 copy hosts/default.nix
envy host select <id>
envy config refine
envy config check
envy apply
```
初始化只创建 host，不选择软件。import 会继续继承 default，copy 会创建独立快照。

## 职责

| 部件 | 负责 |
|---|---|
| install.sh | checkout 与 bootstrap 参数 |
| setup.sh | Nix/runtime 交接 |
| setup.py | 机器配置、软件 policy、secret 保存与加密 |
| envy apply | 求值、激活和持久镜像配置 |
| envy sync/update | 共享分支与 input 的事务更新 |
