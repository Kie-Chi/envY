"""Device and recovery key rotation, recovery-key sealing, and repair."""

from pathlib import Path
from typing import Optional

from envy import log
from envy.utils import (
    AGE_KEY_DIR,
    AGE_KEY_FILE,
    SECRETS_FILE,
    RECOVERY_KEY_FILE,
    run_cmd,
    backup_sensitive_file,
)
from envy.secure_io import atomic_write_text, ensure_private_directory
from envy.keys.storage import (
    current_device_public_key as get_current_device_public_key,
    device_public_keys,
    read_sops_yaml_keys,
    store_device_age_key,
    write_sops_yaml_keys,
)
from envy.keys.identity import ensure_sops_label, get_sops_label
from envy.keys.recovery import (
    decrypt_recovery_key,
    generate_keypair,
    public_key_from_private as _public_key_from_private,
    reencrypt_recovery_key_with as _reencrypt_recovery_key_with,
    write_encrypted_recovery_key as _write_encrypted_recovery_key,
)
from envy.keys import operations


@operations._transactional_key_files
def key_repair() -> None:
    """Repair interrupted rotation markers and revalidate every encrypted document."""
    ensure_private_directory(AGE_KEY_DIR)
    if AGE_KEY_FILE.exists():
        AGE_KEY_FILE.chmod(0o600)
        for backup in AGE_KEY_DIR.glob(AGE_KEY_FILE.name + ".bak*"):
            backup.chmod(0o600)

    keys = read_sops_yaml_keys()
    local_public_keys = device_public_keys()
    current_pub = local_public_keys[-1] if local_public_keys else None
    if not current_pub:
        raise RuntimeError("current device key is not registered in .sops.yaml")
    if current_pub not in keys.values():
        label = get_sops_label()
        old_pub = keys.get(label)
        if not old_pub or old_pub not in local_public_keys:
            raise RuntimeError("current device key is not registered in .sops.yaml")
        keys[f"envy_rotation_old_{label}"] = old_pub
        keys[label] = current_pub
        log.fix("key", "recovered interrupted device rotation marker", label=label)
    if "recovery" not in keys or not RECOVERY_KEY_FILE.exists():
        raise RuntimeError("recovery key metadata or recovery-key.age is missing")

    recovery_private = run_cmd(
        ["age", "--decrypt", "-i", str(AGE_KEY_FILE), str(RECOVERY_KEY_FILE)],
        capture=True,
    )
    recovery_pub = _public_key_from_private(recovery_private)
    pending_recovery = keys.get("recovery_new")
    if pending_recovery:
        if recovery_pub == pending_recovery:
            keys["recovery"] = pending_recovery
            log.fix("key", "completed interrupted recovery-key rotation")
        elif recovery_pub == keys.get("recovery"):
            log.fix("key", "rolled back incomplete recovery-key rotation marker")
        else:
            raise RuntimeError("recovery-key.age matches neither recovery nor recovery_new")
        keys.pop("recovery_new", None)

    old_labels = [
        label for label in keys
        if label.startswith("envy_rotation_old_")
        and label.removeprefix("envy_rotation_old_") in keys
        and keys[label.removeprefix("envy_rotation_old_")] != keys[label]
    ]
    for label in old_labels:
        keys.pop(label)
        log.fix("key", "removed completed device-rotation compatibility key", label=label)

    if recovery_pub != keys.get("recovery"):
        raise RuntimeError("recovery private key does not match .sops.yaml")
    write_sops_yaml_keys(keys)
    _write_encrypted_recovery_key(recovery_private, list(keys.values()))
    if SECRETS_FILE.exists():
        operations.run_sops_updatekeys()
        run_cmd(["sops", "--decrypt", str(SECRETS_FILE)], capture=True)
    log.ok("key", "key repository repaired and verified")


@operations._transactional_key_files
def key_add_recovery(*, reencrypt_secrets: bool = True) -> None:
    keys = read_sops_yaml_keys()
    if "recovery" in keys:
        log.warn("key", "recovery key already exists")
        log.hint("Run: envy key rotate --recovery")
        return
    current_pub = get_current_device_public_key()
    device_recipients = [
        public_key for label, public_key in keys.items()
        if label not in {"recovery", "recovery_new"} and not label.startswith("envy_rotation_old_")
    ]
    if not current_pub or current_pub not in device_recipients:
        raise RuntimeError("an authorized local device key is required before adding recovery")

    priv_line, recovery_pub = generate_keypair()
    _write_encrypted_recovery_key(priv_line, [*device_recipients, recovery_pub])

    keys["recovery"] = recovery_pub
    write_sops_yaml_keys(keys)

    if reencrypt_secrets and SECRETS_FILE.exists():
        operations.run_sops_updatekeys()

    log.ok("key", "recovery key generated and added to .sops.yaml")
    log.hint(f"Recovery public key: {recovery_pub[:30]}...")
    log.info("key", "recovery private key is stored encrypted at secrets/recovery-key.age")
    log.warn("key", "IMPORTANT: also save the recovery key offline (USB/paper/password manager) as ultimate backup")


@operations._transactional_key_files
def key_seal_recovery(priv_path: Optional[str] = None) -> None:
    """Encrypt a recovery private key into secrets/recovery-key.age."""
    keys = read_sops_yaml_keys()
    if "recovery" not in keys:
        raise RuntimeError("no recovery key in .sops.yaml; run: envy key add-recovery")

    if RECOVERY_KEY_FILE.exists():
        log.warn("key", "recovery-key.age already exists, re-encrypting with updated key list")
        operations.reencrypt_recovery_key(keys)
        return

    if priv_path:
        src = Path(priv_path).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"recovery key file not found: {src}")
        recovery_priv = src.read_text().strip()
    else:
        current_pub = get_current_device_public_key()
        if not current_pub:
            raise RuntimeError("no current device key; provide a recovery key path")
        if current_pub != keys["recovery"]:
            raise RuntimeError("current device key is not the recovery key; provide a recovery key path")
        recovery_priv = AGE_KEY_FILE.read_text().strip()

    priv_lines = [l for l in recovery_priv.split("\n") if l.startswith("AGE-SECRET-KEY")]
    if not priv_lines:
        raise ValueError("provided key does not contain AGE-SECRET-KEY")

    priv_line = priv_lines[0]
    derived_pub = _public_key_from_private(priv_line)

    if derived_pub != keys["recovery"]:
        raise ValueError("derived public key does not match recovery key in .sops.yaml")

    _write_encrypted_recovery_key(priv_line, list(keys.values()))

    if SECRETS_FILE.exists():
        operations.run_sops_updatekeys()
    log.ok("key", "recovery private key sealed into secrets/recovery-key.age")
    log.hint("Encrypted for all device keys in .sops.yaml")


def key_recover_recovery(output: Optional[str] = None) -> None:
    """Decrypt the stored recovery private key."""
    if not RECOVERY_KEY_FILE.exists():
        raise FileNotFoundError("no recovery-key.age file found in repo")

    current_pub = get_current_device_public_key()
    if not current_pub:
        raise RuntimeError("no age key on this device to decrypt recovery key")

    decrypted = decrypt_recovery_key()

    if output:
        atomic_write_text(Path(output), decrypted, mode=0o600)
        log.ok("key", "recovery key decrypted and saved", path=output)
    else:
        log.warn("key", "recovery private key:")
        print(decrypted)
        log.hint("Use --output to save to a file safely.")


@operations._transactional_key_files
def key_rotate(recovery: bool = False) -> None:
    """Rotate current device key or recovery key."""
    keys = read_sops_yaml_keys()

    if recovery:
        current_pub = get_current_device_public_key()
        if not current_pub or current_pub not in keys.values():
            raise RuntimeError("current device key must be in .sops.yaml to rotate recovery key")

        old_recovery_pub = keys.get("recovery")
        if not old_recovery_pub:
            raise RuntimeError("no recovery key to rotate; run: envy key add-recovery")

        log.step("key", "rotating recovery key")
        log.hint(f"Old recovery: {old_recovery_pub[:30]}...")

        priv_line, new_recovery_pub = generate_keypair()

        keys["recovery_new"] = new_recovery_pub
        write_sops_yaml_keys(keys)
        operations.run_sops_updatekeys()

        recipients = [v for k, v in keys.items() if k not in {"recovery", "recovery_new"}]
        _write_encrypted_recovery_key(priv_line, recipients)

        keys.pop("recovery")
        keys["recovery"] = new_recovery_pub
        keys.pop("recovery_new")
        write_sops_yaml_keys(keys)
        operations.run_sops_updatekeys()
        log.ok("key", "recovery key rotated", pubkey=new_recovery_pub[:30] + "...")
        log.warn("key", "IMPORTANT: save the new recovery key offline (USB/paper/password manager)")
        return

    # Rotate device key
    current_pub = get_current_device_public_key()
    if not current_pub:
        raise RuntimeError("no current key to rotate; run: envy key import")

    label = ensure_sops_label()
    if current_pub not in keys.values():
        raise RuntimeError("current device key not in .sops.yaml, cannot rotate")
    if label in {"recovery", "recovery_new"} or label.startswith("envy_rotation_old_"):
        raise RuntimeError("device sops label is reserved")
    if keys.get(label) != current_pub:
        raise RuntimeError(f"sops key label '{label}' does not match the current device key")

    if "recovery" not in keys:
        raise RuntimeError("no recovery key in .sops.yaml; rotation requires recovery")

    log.step("key", f"rotating key for device '{label}'")
    log.hint(f"Old public key: {current_pub[:30]}...")

    old_key_content = AGE_KEY_FILE.read_text().strip()

    new_key_content, new_pub = generate_keypair()

    log.hint(f"New public key: {new_pub[:30]}...")

    store_device_age_key(old_key_content + "\n" + new_key_content)

    old_label_temp = f"envy_rotation_old_{label}"
    keys[old_label_temp] = current_pub
    keys[label] = new_pub
    write_sops_yaml_keys(keys)
    operations.run_sops_updatekeys()

    # Remove the old device recipient before publishing the final recovery
    # ciphertext. A retired device must not retain access through recovery.
    final_keys = dict(keys)
    final_keys.pop(old_label_temp)
    _reencrypt_recovery_key_with(final_keys)

    keys.pop(old_label_temp)
    write_sops_yaml_keys(keys)
    operations.run_sops_updatekeys()

    backup_sensitive_file(AGE_KEY_FILE)
    store_device_age_key(new_key_content)

    log.ok("key", f"key rotation complete for '{label}'")
    log.hint(f"New public key: {new_pub}")
