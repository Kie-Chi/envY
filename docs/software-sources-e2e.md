# Software source E2E verification

2026-10-13 的验收覆盖真实 CLI、Nix 求值与事务，以及一次性 Linux 容器中的
APT 签名索引和安装。入口是
`resources/scripts/tests/e2e_software_sources.py`；它不属于默认单元测试发现范围。

## CLI / Nix

```bash
nix develop path:. --command python -B resources/scripts/tests/e2e_software_sources.py cli --report /tmp/envy-source-e2e-cli.json
```

这组测试复制当前 tracked/untracked 非忽略文件到临时 checkout，只在副本中
写入机器配置、flake 和 lock。envY 缓存与 state 使用临时目录，真实 Nix
继续使用本机 store 与 fetcher cache。测试用的 HTTPS 来源有独立证书，证书
配置只传给测试子进程；不修改主机信任或全局 Nix registry。

已通过的五个流程：

| 流程 | 实际核验 |
|---|---|
| 锁定搜索与软件生命周期 | 临时 global registry 的 hello 版本为 999，envY 仍搜索项目锁定的 hello；首次添加、dry-run、离线索引命中、保存后重新求值、clean 移除 |
| 机器自定义 registry | HTTPS PyPI metadata 精确解析，来源定义与 package 写入，仍有有效依赖时禁止删除来源，清理软件后可删除 |
| 共享来源成功事务 | HTTPS flake tarball 登记、生成 lock、全机器检查、来源包添加、依赖删除保护、软件与 input 移除 |
| 共享来源失败事务 | 可正常锁定但没有 package 输出的 flake 被拒绝，flake.nix 和 flake.lock 字节级恢复，stdout 保持单一 JSON 文档 |
| 单平台 flake | 只提供 Linux package 输出的来源可登记，目录正确报告 platforms 与当前机器 ready 状态 |

## Linux native / artifact

```bash
docker build -t envy-source-e2e -f resources/scripts/tests/Dockerfile.software-sources-e2e resources/scripts/tests
docker run --rm --env DEBIAN_FRONTEND=noninteractive --mount type=bind,src="$PWD",dst=/workspace,readonly envy-source-e2e
```

Dockerfile 默认使用 Ubuntu 24.04，也可用
`--build-arg BASE_IMAGE=python:3.11-slim` 等支持 APT 的基础镜像。本次实际使用
本机已缓存的 Debian 13 / aarch64 镜像构建并运行，通过一个完整 native 流程。
测试有 root container 检查；不要将 native 入口用于普通 Linux 主机。

测试在容器内生成公钥、HTTPS 证书、两个签名仓库和微型 deb 包：默认源
提供同名包 9.0，厂商源提供 1.0。实际结果包括：

- 有效 source set 用镜像替换可识别的发行版源，同时保留外部厂商源。
- 默认 APT candidate 为 9.0，指定厂商 source 的精确解析和安装结果为 1.0。
- 直接 artifact CLI 下载、核验身份并通过 APT 安装固定内容；哈希和包名错误被拒绝。
- Native 与 artifact 安装入口在禁用 Python site-packages 的子进程中运行，核对 Nix 激活所用的纯 Python 运行时。
- 同 URI 的有效外部源被保留，envY 自有 descriptor/keyring 可创建、清理。
- 错误公钥指纹、篡改的 InRelease 被拒绝；准备失败不写入仓库文件。
- 私有索引更新不执行系统 APT update hooks。

容器以只读方式挂载工作区，安装与卸载只涉及容器中的 fixture 包。结束后
容器自动删除。此结果不代表已经激活用户的 Linux 机器。

## 其他验证

Python 单元测试 341 项、Rust TUI 测试 51 项通过。三个版本化机器目标均
通过 Nix derivation 求值；Darwin/Linux 平台边界与 Linux policy checks 通过。
DNF/Zypper/Pacman 的 parser 与能力限制由 fixtures 覆盖；其独立系统安装
E2E 不在本次已验证范围，软件目录会准确报告未实现的能力。
