# Key Management (envy key)

sops-nix 使用 age 加密管理 envY 仓库中的私密信息。`envy key` 提供密钥全生命周期管理——从导入、轮换、添加/移除设备，到恢复密钥的生成与提取。

## 快速诊断

```bash
envy key status    # 检查当前设备密钥状态：是否存在、是否在 .sops.yaml 中、能否解密
envy key list      # 查看 .sops.yaml 中所有密钥及当前设备标记
envy key repair    # 修复权限或中断轮换标记，并验证 secrets/recovery 密文
```

## 密钥架构

密钥体系由两部分组成：

| 类型 | 作用 | 位置 |
|------|------|------|
| **设备密钥** | 每台设备拥有独立的 age 私钥，用于日常解密 | Darwin: `~/Library/Application Support/sops/age/keys.txt`; Linux: `~/.config/sops/age/keys.txt` |
| **恢复密钥** | 独立于任何设备，用于密钥轮换和设备丢失时恢复访问 | 私钥加密存储在 `secrets/recovery-key.age`，公钥在 `.sops.yaml` 中 |

`.sops.yaml` 使用设备标签（基于 hostname）作为 YAML anchor：

```yaml
keys:
  - &macbook_air age1xxx...    # 设备密钥
  - &recovery age1yyy...       # 恢复密钥
creation_rules:
  - path_regex: secrets/secrets\.yaml$
    key_groups:
      - age:
          - *macbook_air
          - *recovery
```

每台设备的 machine target 与 sops key label 统一存储在 Git 忽略的 `.device-label` TOML 中。`device.sops_label` 默认为 hostname 的 sanitized 版本，并可独立于 `device.machine_id`。
`envy setup` 在已有设备密钥时会确保该标记被持久化；如果本地标记丢失但当前公钥仍能在 `.sops.yaml` 中匹配到设备标签，会优先恢复该标签。

## 常见场景

### 场景 1：新设备配置

在新设备上克隆 envY 仓库后，sops 无法解密 `secrets.yaml`。有两种方式：

**方式 A — 从旧设备复制密钥（最推荐）：**

1. 在旧设备上导出密钥到 USB 的 `recovery/` 目录：
   ```bash
   envy key ex -o /Volumes/USB/recovery/key.age
   ```
2. 在新设备上插入 USB，运行：
   ```bash
   envy key im    # 交互模式——自动扫描 USB，列出找到的密钥文件让你选择
   ```
   `envy key im` 会自动扫描所有已挂载的 USB（macOS: `/Volumes`，Linux: `/media/$USER` + `/mnt`），优先在 `recovery/` 子目录查找 `.age` / `.age.txt` 文件。选择后无需手动输入路径。

   也可以直接指定路径：
   ```bash
   envy key im -a /Volumes/USB/recovery/key.age -l new_macbook
   ```

**方式 B — 从 SSH 密钥派生（如果旧设备也用 SSH 派生）：**

```bash
envy key im -s ~/.ssh/id_ed25519 -l new_macbook
```

**方式 C — 全新密钥 + 重新输入所有秘密：**

```bash
envy key im -g -l new_macbook
envy setup   # 重新填写所有秘密值
```

如果不需要恢复旧设备的秘密值，可以直接跳过旧 age key：

```bash
bash setup.sh --reset-secrets
```

setup 会让 secret 字段从空值开始，并在保存时整体替换 `secrets/secrets.yaml`；旧密文不会被合并。

导入后 `envy key import` 会自动将新密钥加入 `.sops.yaml` 并运行 `sops updatekeys`，使新设备能解密已有秘密。

也可以通过 `envy setup` 交互式完成——如果解密失败，它会自动进入密钥导入流程。

### 场景 2：添加另一台设备的密钥

如果你想在另一台设备上也能解密 secrets，需要把那台设备的公钥加入 `.sops.yaml`：

1. 在另一台设备上查看公钥：
   ```bash
   envy key status   # 查看 Public key
   ```
2. 在任意设备上添加：
   ```bash
   envy key add age1xxx... --label work_desktop
   ```

这会自动 re-encrypt secrets 和 recovery key 给新设备。

### 场景 3：移除丢失设备的密钥

如果一台设备丢失或不再需要：

```bash
envy key remove work_desktop
```

移除后会 re-encrypt secrets，被移除的设备将不再能解密。

注意：不能移除 `recovery` 密钥、不能移除当前设备密钥（除非 `--force`）、不能移除最后一个设备密钥。

### 场景 4：密钥轮换

如果怀疑密钥泄露，或想定期更换：

```bash
envy key rotate
```

轮换流程（两阶段，确保零数据丢失）：

1. **Phase 1**: 在 `keys.txt` 中追加新密钥（保留旧密钥），`.sops.yaml` 同时包含新旧密钥，`sops updatekeys` re-encrypt 给旧+新+恢复
2. **Phase 2**: 从 `.sops.yaml` 和 `keys.txt` 中移除旧密钥，再次 `sops updatekeys`

整个过程中，恢复密钥始终在场，确保即使旧密钥出问题也不会丢失秘密。

轮换恢复密钥：
```bash
envy key rotate --recovery
```

### 场景 5：恢复密钥

**生成恢复密钥**（首次 setup 时如果没有，需要手动生成）：
```bash
envy key add-recovery
```

恢复密钥的私钥会被 age 加密后存储在 `secrets/recovery-key.age`，加密目标为 `.sops.yaml` 中的所有设备公钥。任何已有设备都能解密它。

**提取恢复密钥私钥**（用于离线保存或紧急恢复）：
```bash
envy key recover-recovery               # 打印到终端
envy key recover-recovery --output /tmp/recovery.txt   # 保存到文件
```

强烈建议将恢复密钥私钥额外保存在离线位置（USB、纸质、密码管理器），作为设备全部丢失时的终极备份。

### 场景 6：导出密钥用于传输

```bash
envy key ex                  # 输出 age 私钥到 stdout
envy key ex -o key.txt       # 保存到文件
envy key ex -F ssh           # 导出 SSH 私钥（仅适用于 SSH 派生的设备）
```

推荐导出到 USB 的 `recovery/` 目录，这样新设备 `envy key im` 时会自动识别：
```bash
envy key ex -o /Volumes/USB/recovery/macbook_air.age
```

私钥是敏感数据，请使用安全传输方式（物理 USB 比网络传输更安全）。

## 内部机制

### USB 自动扫描

`envy key im`（交互模式）会自动扫描已挂载的 USB 驱动器寻找密钥文件：
- macOS: `/Volumes` 下所有挂载点
- Linux: `/media/$USER` + `/mnt`

扫描策略：
1. **优先**：每个挂载点的 `recovery/` 子目录下的 `.age` / `.age.txt` 文件
2. **其次**：挂载点根目录下的 `.age` / `.age.txt` 文件

找到的文件会按优先级列出，用户直接选择编号即可导入，无需手动输入路径。

推荐约定：在 USB 上创建 `recovery/` 目录存放密钥文件，`envy key ex -o /Volumes/USB/recovery/key.age`。

### SOPS_AGE_KEY_FILE

Linux 使用 sops 默认的 `~/.config/sops/age/keys.txt`，Darwin 使用 `~/Library/Application Support/sops/age/keys.txt`。`envy.utils.run_cmd()` 会在所有 sops/age 命令中设置对应平台的 `SOPS_AGE_KEY_FILE`。

### Secret 写入与事务安全

`envy.config.write_secrets_data()` 不会把明文写到 Git 跟踪的
`secrets/secrets.yaml`：

1. 在 `secrets/` 中创建权限为 `0600` 的私有临时明文文件。
2. 让 sops 把它加密到另一个 prepared 临时文件。
3. 验证输出存在 sops metadata 且不是空文件。
4. `fsync` 后原子替换正式密文；无论成功或失败都清理临时明文。

更新已存在的 secrets 时默认合并原 YAML，schema 尚未认识的扩展路径也会保留。
只有显式使用内部 `replace=True` 才会重建整份数据。

Setup 与 key lifecycle 使用多文件事务，同时覆盖 machine、`.sops.yaml`、
`secrets.yaml`、`recovery-key.age`、本机 age key 和 `.device-label`。任一步失败会把
所有参与文件恢复到操作前状态。age 私钥、导出和恢复文件使用 `0600`，age 目录
使用 `0700`；`recovery-key.age` 也是先生成 prepared 密文再原子替换。

Key 命令只在文件事务成功结束后才进入 scoped Git stage/commit。Git 提交失败不会
把已经验证过的密钥文件回滚成与 index 不一致的旧版本；命令会返回失败，保留一致
的工作区供用户检查并重试提交。没有实际 repository 文件变化的 no-op 不会触发提交。

### 中断恢复

轮换过程使用临时 recipient 标记：设备轮换为 `<label>_old`，恢复密钥轮换为
`recovery_new`。如果进程在两阶段之间终止，运行：

```bash
envy key repair
```

Repair 会修复 age 文件权限，核对 recovery private/public key，判定应完成还是回退
`recovery_new`，删除已完成的 `_old` 兼容项，重新加密并验证
`secrets.yaml` 与 `recovery-key.age`。无法证明安全状态时命令失败并由事务回滚。

### 密钥文件多行支持

age 的 `keys.txt` 可以包含多行 `AGE-SECRET-KEY-...`（多个私钥）。密钥轮换时利用此特性：Phase 1 在文件中追加新私钥，使 sops 可以同时用旧密钥和新密钥解密。

## 子命令速查

| 命令 | 简写 | 用途 |
|------|------|------|
| `envy key list` | `ls` | 显示所有密钥 |
| `envy key status` | `st` | 诊断当前设备状态 |
| `envy key repair` | — | 修复中断轮换、权限并验证密文 |
| `envy key add PUBKEY` | `a` | 添加设备公钥 |
| `envy key remove LABEL` | `rm` | 移除设备密钥 |
| `envy key export` | `ex` | 导出当前设备私钥 |
| `envy key import` | `im` | 导入/生成密钥 |
| `envy key rotate` | — | 轮换密钥 |
| `envy key add-recovery` | `ar` | 生成恢复密钥 |
| `envy key recover-recovery` | `rr` | 提取恢复密钥私钥 |

常用选项简写：`-o` 输出文件，`-l` 标签，`-f` 强制，`-a` age 密钥路径，`-s` SSH 路径，`-g` 生成新密钥，`-r` 轮换恢复密钥，`-F` 导出格式。

示例：
```bash
envy key ls                          # 查看密钥列表
envy key st                          # 诊断状态
envy key ex -o USB/key.txt           # 导出到 USB
envy key im -a USB/key.txt -l laptop # 从 USB 导入
envy key rm work_desktop             # 移除密钥
envy key rr -o /tmp/recovery.txt     # 提取恢复密钥
