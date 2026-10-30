"""Import, derive, or generate the age key of this device."""

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
from envy.secure_io import ensure_private_directory
from envy.process import CommandError
from envy.keys.storage import (
    current_device_public_key as get_current_device_public_key,
    prepare_device_age_key,
    prepare_generated_device_age_key,
    commit_prepared_device_age_key,
    read_sops_yaml_keys,
)
from envy.keys.recovery import decrypt_recovery_key
from envy.keys import operations, prompts, rotation


def warn_if_device_key_is_recovery(pub: str, keys: dict) -> bool:
    """Warn (non-blocking) when the device key equals the recovery key.

    Using the recovery key as the device key defeats the offline-backup
    separation the recovery key exists for. Returns True on overlap so callers
    can offer remediation (rotation).
    """
    if pub and keys.get("recovery") == pub:
        log.warn("key", "your device key is the same as the recovery key")
        log.hint("The recovery key should be kept separate for offline backup.")
        log.hint("Recommended: run 'envy key rotate' to generate an independent device key.")
        return True
    return False


@operations._transactional_key_files
def key_import(
    age_path: Optional[str] = None,
    ssh_path: Optional[str] = None,
    generate: bool = False,
    label: Optional[str] = None,
    register: bool = True,
    prepared: bool = False,
) -> Optional[str]:
    """Import a key for the current device. Returns public key on success."""
    pub = None
    staged_key = None

    # If AGE_KEY_FILE already exists, confirm overwrite and backup before any write path
    if register and not prepared and AGE_KEY_FILE.exists() and (age_path or ssh_path or generate):
        if not prompts.confirm(f"AGE_KEY_FILE already exists at {AGE_KEY_FILE}. Overwrite?"):
            log.warn("key", "import cancelled")
            return None
        backup_sensitive_file(AGE_KEY_FILE)

    if age_path:
        src = Path(age_path).expanduser()
        if not src.exists():
            log.error("key", "file not found", path=str(src))
            return None
        content = src.read_text().strip()
        try:
            staged_key = prepare_device_age_key(content)
        except (ValueError, RuntimeError) as exc:
            log.error("key", str(exc))
            return None
        pub = staged_key.public_key
        if pub:
            log.ok("key", "age key imported", pubkey=pub[:20] + "...")
    elif ssh_path:
        src = Path(ssh_path).expanduser()
        if not src.exists():
            log.error("key", "file not found", path=str(src))
            return None
        ensure_private_directory(AGE_KEY_DIR)
        result = run_cmd(["ssh-to-age", "-private-key", "-i", str(src)], capture=True)
        staged_key = prepare_device_age_key(result)
        # Derive the public key from the converted private identity. The SSH
        # .pub file is optional and may be stale or absent on a new machine.
        pub = staged_key.public_key
        if pub:
            log.ok("key", "age key derived from SSH", pubkey=pub[:20] + "...")
    elif generate:
        staged_key = prepare_generated_device_age_key()
        pub = staged_key.public_key
        if pub:
            log.ok("key", "new age key generated", pubkey=pub[:20] + "...")
    else:
        # Interactive mode — arrow-key navigation menu
        usb_files = prompts.scan_usb_key_files()

        items = []
        for f in usb_files:
            tag_text = "USB " if f.parent.name == "recovery" else "USB "
            path_text = str(f)
            items.append(([("usb", tag_text), ("", path_text)], "usb", str(f)))
        items.append(([("", "Import age key file (specify path)")], "age_manual", ""))
        items.append(([("", "Import SSH key and derive age key")], "ssh_manual", ""))
        items.append(([("", "Generate new age key")], "generate", ""))
        items.append(([("", "Quit")], "quit", ""))

        result = prompts._select_menu(items, title="Import age key for this device")
        if result is None or result[0] == "quit":
            return None

        action, data = result
        if action == "usb":
            return key_import(age_path=data, label=label, register=register, prepared=prepared)
        elif action == "age_manual":
            default_path = "~/Library/Application Support/sops/age/keys.txt"
            try:
                path = prompts.prompt_input(f"Path to age key file [{default_path}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not path:
                path = default_path
            return key_import(age_path=path, label=label, register=register, prepared=prepared)
        elif action == "ssh_manual":
            default_ssh = "~/.ssh/id_ed25519"
            try:
                path = prompts.prompt_input(f"Path to SSH private key [{default_ssh}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not path:
                path = default_ssh
            return key_import(ssh_path=path, label=label, register=register, prepared=prepared)
        elif action == "generate":
            return key_import(generate=True, label=label, register=register, prepared=prepared)

    if not pub:
        log.error("key", "key import failed")
        return None

    previous_secrets = None
    previous_recovery_private = None
    if register:
        if SECRETS_FILE.exists():
            from envy.config.secret_store import read_secrets_data
            old_data, old_ok = read_secrets_data()
            if old_ok:
                previous_secrets = old_data
        if RECOVERY_KEY_FILE.exists() and AGE_KEY_FILE.exists():
            try:
                previous_recovery_private = run_cmd(
                    ["age", "--decrypt", "-i", str(AGE_KEY_FILE), str(RECOVERY_KEY_FILE)],
                    capture=True,
                )
            except (CommandError, OSError, RuntimeError, ValueError):
                pass

    if not register:
        log.ok("key", "device key prepared for setup", pubkey=pub[:20] + "...")
        return staged_key

    if staged_key is not None:
        try:
            commit_prepared_device_age_key(staged_key)
        finally:
            # The private staging file must not survive a failed registration,
            # recipient update, or later sops operation.
            staged_key.cleanup()

    # Set the sops key label in shared device metadata.
    # Register the recipient after the private identity is active. Re-encrypt
    # old ciphertext only when this identity can actually decrypt it; a new
    # device may be registering with an old ciphertext that must be resealed by
    # an already-authorized device.
    current_label = operations.register_device_key(pub, label)
    keys = read_sops_yaml_keys()
    if previous_recovery_private is not None:
        from envy.keys.recovery import write_encrypted_recovery_key
        write_encrypted_recovery_key(previous_recovery_private, keys.values())
    elif RECOVERY_KEY_FILE.exists():
        try:
            decrypt_recovery_key()
            operations.reencrypt_recovery_key(keys)
        except (CommandError, OSError, RuntimeError, ValueError):
            log.warn("key", "registered device cannot decrypt existing recovery-key.age; left it unchanged")
    if previous_secrets is not None:
        from envy.config.secret_store import write_secrets_data
        write_secrets_data(previous_secrets)
    elif SECRETS_FILE.exists():
        from envy.config.secret_store import read_secrets_data
        _, secrets_accessible = read_secrets_data()
        if secrets_accessible:
            operations.run_sops_updatekeys()
        else:
            log.warn("key", "registered device cannot decrypt existing secrets.yaml; left it unchanged")
    log.ok("key", f"key imported and added to .sops.yaml as '{current_label}'")
    log.hint(f"Public key: {pub}")

    # Warn if recovery public key exists but recovery-key.age is missing
    if "recovery" in keys and not RECOVERY_KEY_FILE.exists():
        log.warn("key", "recovery key is in .sops.yaml but secrets/recovery-key.age is missing")
        log.hint("Without recovery-key.age, you cannot export (envy key rr) the recovery private key from this device.")

    # Warn if device key overlaps with recovery key
    keys = read_sops_yaml_keys()
    if warn_if_device_key_is_recovery(pub, keys):
        if not RECOVERY_KEY_FILE.exists():
            log.info("key", "the recovery private key needs to be sealed into secrets/recovery-key.age")
            log.hint("Sealing encrypts the recovery private key with all device public keys")
            log.warn("key", "if you skip sealing now, the recovery private key will be permanently lost after rotation")
            if prompts.confirm("Seal recovery key into recovery-key.age?"):
                rotation.key_seal_recovery()
            else:
                log.hint("Run: envy key seal-recovery")

        if prompts.confirm("Rotate device key now?"):
            rotation.key_rotate()
            new_pub = get_current_device_public_key()
            log.ok("key", "device key rotated", pubkey=new_pub)
            return new_pub

    return pub
