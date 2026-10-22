# TUI 前端边界

启动：

```bash
envy tui
```
Rust/Ratatui 前端只负责展示和交互，通过 envY CLI 的 JSON boundary 调用后端；policy、事务、镜像、软件来源和 secret 规则仍由 Python/Nix backend 所有。

## JSON 约束

- 请求和响应使用 resources/scripts/envy/jsonio.py 的稳定 envelope。
- 前端不能直接读写 host、source、lock、secret 或应用配置。
- 失败必须保留 backend 的错误码和可操作信息；不能把 secret-safe 状态扩展成敏感值。
- 新命令先实现 CLI，再接入 TUI；两者不得各自实现 policy。

```bash
cargo test --manifest-path resources/scripts/envy-tui/Cargo.toml
python3 -m compileall -q resources/scripts/envy
```
