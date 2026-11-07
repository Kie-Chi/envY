---
name: envy-maintain
description: "修改 envY 的 Nix 选项、机器软件政策、Python CLI、Rust TUI 或 JSON 契约时，按影响范围选择并执行现有验证。用于实现阶段的验证选路；故障根因调查用 hunt，diff、PR 和发布审查用 check。"
---

# envY 改动验证

先读仓库 `AGENTS.md`、相关目录约定及被改代码，检查 `git status --short` 和现有 diff。沿用用户已批准的计划与范围，不为已授权的常规实现重复确认。本技能只补充验证选路，不复制项目规则，也不替代 hunt 的根因调查或 check 的审查流程。

以下路径均相对 envY 仓库根目录；从安装后的 skill 目录操作时，先定位实际 checkout。先确认当前 `flake.nix` 与 `.github/workflows/envy-checks.yml` 中的检查定义，避免沿用过期命令。

## 按改动选择验证

| 影响范围 | 先检查的实现与契约 | 对应验证 |
|---|---|---|
| Nix 选项、跨平台模块、机器 manifest | `modules/envy/options.nix`、`selection-options.nix`、`manifest.nix`、`darwin.nix`、`linux.nix`；机器文件与 Python schema 的字段对应 | 先对受影响机器运行 `envy host check <id>`；公共平台边界改动选 `platform-option-boundaries`，Linux 桌面/服务器政策选 `linux-policy-boundaries` |
| 软件 include/exclude、源与机器 managed blocks | `hosts/<platform>/<id>.nix`、`resources/scripts/envy/software/`、`sources/`、`config/machine_nix.py` | 选择 `test_software*.py`、`test_software_sources.py`、`test_machine_config.py` 或 `test_host.py` 中相关测试；政策边界再补对应机器求值 |
| Python CLI、setup、进程或事务 | `resources/scripts/envy/`、`setup.py` 及 `resources/scripts/tests/` 对应模块 | 先运行对应 unittest 模块；跨模块影响较大时运行 `envy-python-tests`；静态检查选 `envy-lint` |
| Rust TUI 展示、导航与命令调用 | `resources/scripts/envy-tui/src/`；TUI 调用的 CLI JSON 入口 | 对应 Cargo 测试；完整现有入口为 `envy-tui-tests`，其 Nix 包启用 `doCheck` |
| CLI/TUI JSON 契约 | `resources/scripts/envy/jsonio.py`、各 CLI JSON 分支与 TUI 解析代码 | `test_structured_output.py` 及相关命令测试，再运行受影响的 Rust 测试；核对 `schemaVersion`、`command`、`ok`、`data`、`warnings` 和失败时的 `error` |
| 启动脚本、shell 片段 | `envy`、`install.sh`、`setup.sh`、`requires.sh` 及实际被改脚本 | 先 `bash -n` / 对应 shell 语法检查；完整入口为 `envy-shell-checks` |
| agent wrapper、工具或 skill 注册 | `modules/agents/`、`checks/agent-skills.nix`、`docs/agents.md` | wrapper/工具选 `envy-agent-tests`；skill discovery、adapter 和 manifest 边界选 `platform-option-boundaries`；Pi 宿主/扩展改动再用 pi-maintain |
| secret 声明、schema、权限或模板 | owning feature、`resources/scripts/envy/config/`、`schemas/` | 对应 secret/schema 测试与 `secret-safety`；不读取、打印真实凭据来验证 |

测试文件名用于定位，运行前查看实际测试的 fixture 和依赖。`test_structured_config.py` 验证运行时 JSON/TOML 合并，并不代替 CLI JSON envelope 测试。JSON 改动需同时验证成功与错误路径、stdout 可解析性以及消费者兼容性；保持 TUI 只负责展示，不在 Rust 中重建政策。

## 执行入口

在仓库根目录运行；`system` 使用当前机器对应的 `aarch64-darwin` 或 `x86_64-linux`。例如：

```bash
nix build --no-link path:.#checks.aarch64-darwin.platform-option-boundaries
nix build --no-link path:.#checks.aarch64-darwin.envy-python-tests
nix build --no-link path:.#checks.aarch64-darwin.envy-tui-tests
```

只选本次改动需要的入口，不将这三个命令作为每次任务的固定组合。Linux 专有检查入口为 `checks.x86_64-linux.linux-policy-boundaries`。其余表中的名字同样位于 `checks.<system>.<name>`。

快速定向验证可复用 devShell：

```bash
nix develop path:. --command python -m unittest discover -s resources/scripts/tests -p 'test_structured_output.py'
CARGO_TARGET_DIR="$HOME/.cache/envy/cargo-target" nix develop path:. --command cargo test --manifest-path resources/scripts/envy-tui/Cargo.toml
```

确认 Python 字节码和 Cargo 构建缓存位于 `/tmp` 或 `~/.cache/envy`；devShell 与 envY wrapper 已有相应默认设置。会修改机器政策或用户状态的测试必须使用现有临时 fixture；不要把真实 `envy sw add/rm`、`config refine` 当测试。

## 扩大验证与交付

局部检查通过后，仅在公共选项、跨平台组合、共享源注册或未解决影响要求时扩大到 `envy check --changed` / `envy check --all` 或 `nix flake check path:. --no-build`。需要验证构建行为时再选择对应 `nix build --no-link` 或 `envy check --build`；小改动不默认全量构建。

CI 会先执行 `nix flake check path:. --no-build`，随后分别构建 Python、TUI、shell、lint、agent、Pi 回归和边界检查。只完成求值不代表这些构建测试已执行。

结束时报告改动文件、实际运行的命令与结果，以及未验证的平台或行为。验证不自动执行 `envy apply`，不隐式创建 goal 或文档报告，也不执行 commit/push。
