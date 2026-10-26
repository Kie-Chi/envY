# Mirror policy

镜像是 machine setting，由 envy.mirrors.mode 和可选的生态覆盖组成：

- china：使用 catalog 中审计过的国内端点。
- upstream：使用上游服务；Linux APT 回到系统已有 source。

```nix
envy.mirrors.mode = "china";
```
覆盖由 envy mirror set <target> <source> 写入 host 的 managed block，profile 仍是未覆盖 target 的 fallback。reset 只删除该 target 的 envY 条目。

## 两阶段

1. Bootstrap：install.sh 设置 ENVY_MIRROR，setup.sh 注入临时 npm、PyPI/uv、Go、Rust/Cargo 环境；nix-trust.sh 设置 daemon trusted substituters。flake-source-mirror.sh 可依据 lock fingerprint 为受支持的 GitHub source 选择镜像。
2. Apply：setup 写入 machine policy；apply 由 modules/mirrors 生成长期用户/daemon 环境、Homebrew 或 APT 配置。bootstrap 与 declarative catalog 必须一致。

## ownership

| 范围 | 规则 |
|---|---|
| Nix cache | 保留签名校验；china 使用 catalog 中的缓存，upstream 使用上游缓存 |
| Flake source | 只处理 lock 中有 narHash 的 GitHub/GitHub API source；内容校验失败回退原 URL |
| npm/PyPI/uv/Go/Rust/Cargo | 使用 catalog 端点；覆盖属于 machine policy |
| Darwin Homebrew | 由 Darwin activation 管理 installer、API、bottle 和 catalog 中的 artifact mirror |
| Linux APT | envY 只管理自己的 /etc/apt/sources.list.d/envy-mirror.sources；不改系统或第三方 source |
| Docker 等外部安装器 | 仅使用 catalog 中明确声明的 mirror 参数 |
| 容器基础镜像 | amux 构建镜像时用 `docker.registry` 替换 docker.io；覆盖写在 machine 文件的 `envy.mirrors.overrides.docker.registry`，不修改 Docker daemon 配置 |

APT 搜索、更新、安装与本地 artifact 依赖共用有效 source 集合；切回 upstream 只删除 envY 自己的文件。

## Flake source mirror

它不同于 binary cache。source mirror 读取 flake.lock，只替换已锁定且有 narHash 的支持 source；下载内容由 Nix hash 验证。失败节点保留原始锁定 URL 作为回退，缓存写入 XDG_CACHE_HOME 或 ~/.cache/envy/flake-sources，不改 lock。

```bash
ENVY_FLAKE_MIRROR_URL=https://trusted.example/ envy check
ENVY_FLAKE_SOURCE_MIRROR=0 envy check
ENVY_MIRROR=upstream envy check
```
固定 hash 的其他下载地址、含 token 的 URL 和未知代理不进入通用替换。

## 查看与选择

```bash
envy mirror status
envy mirror probe
envy mirror targets
envy mirror sources npm
envy mirror measure npm --provider chsrc
envy mirror set npm npmmirror --dry-run
envy mirror reset npm --yes
envy mirror cache status
envy doctor network
```
