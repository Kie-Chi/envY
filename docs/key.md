# 密钥管理

sops-nix 用 age 加密 secrets/secrets.yaml。Nix eval 只能看到 secret 文件路径或 template placeholder，不能看到明文。

```bash
envy key status
envy key list
envy key repair
```
## 文件与角色

- Darwin device key：~/Library/Application Support/sops/age/keys.txt
- Linux device key：~/.config/sops/age/keys.txt
- recovery key：secrets/recovery-key.age
- recipients/labels：.sops.yaml 与 Git 忽略的 .device-label

新设备先生成或导入 age key，再将 public key 加入 .sops.yaml 并运行 sops updatekeys secrets/secrets.yaml。移除设备也必须重新加密；丢失设备立即移除其 recipient。

## 轮换与恢复

轮换分两阶段：先让 keys.txt 同时包含旧、新私钥，并用旧、新、recovery recipients 重加密；验证新设备可解密后，再移除旧 recipient 和私钥并再次重加密。恢复 key 用于设备丢失和无法访问时的恢复，不作为日常配置来源。

```bash
sops --decrypt secrets/secrets.yaml
sops updatekeys secrets/secrets.yaml
envy key repair
```
## 写入安全

setup/config writer 在 secrets/ 创建权限为 0600 的临时明文，交给 sops 加密到临时文件，验证 metadata 后原子替换 tracked ciphertext。任何失败都回滚 machine、.sops.yaml、secret、recovery、device label 和本地 key 的事务。

禁止提交明文 secrets/secrets.yaml，禁止在日志、摘要、manifest、doctor 或 Nix expression 中打印 secret。密钥生命周期与 mirror/source 更新都必须保留可恢复的事务边界。
