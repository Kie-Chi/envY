# 软件来源 E2E 验证

入口：resources/scripts/tests/e2e_software_sources.py。该测试不在默认 unit test discovery 中。

## 覆盖范围

- CLI：source list/show/search/add/rm/sync、manifest 解析、精确 identity cache 和事务回滚。
- Nix：source binding、全部机器 target 求值和 lock 变更回滚。
- Linux native/artifact：APT 签名索引、envY source ownership、安装和 hash 校验。
- secret safety：错误路径不打印 token 或 secrets 明文。

## 运行

```bash
python3 resources/scripts/tests/e2e_software_sources.py
nix flake check path:. --no-build
envy check --all
```
环境缺少容器、Nix 或 provider 时，记录跳过原因；不要用未验证的网络结果替代本地 lock、manifest 和 source identity 检查。
