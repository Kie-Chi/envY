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
    validate_device_label,
    write_sops_yaml_keys,
)
from envy.keys.identity import sanitize_label, set_sops_label
from envy.keys.recovery import decrypt_recovery_key
from envy.keys import operations, prompts


def _device_label_of(public_key: Optional[str], keys: dict[str, str]) -> Optional[str]:
    """Return the device label registered for a public key, ignoring reserved entries."""
    if not public_key:
        return None
    labels = [
        label for label, value in keys.items()
        if value == public_key
        and label not in {"recovery", "recovery_new"}
        and not label.startswith("envy_rotation_old_")
    ]
    return labels[0] if len(labels) == 1 else None


def _replace_device_key(previous_label: str, public_key: str, label: Optional[str]) -> str:
    """Move a device's recipient slot to its new key, dropping the old key."""
    keys = read_sops_yaml_keys()
    new_label = validate_device_label(sanitize_label(label) if label else previous_label)
    if new_label != previous_label and new_label in keys:
        raise ValueError(f"sops key label '{new_label}' belongs to another age key")
    write_sops_yaml_keys({
        (new_label if name == previous_label else name): (public_key if name == previous_label else value)
        for name, value in keys.items()
    })
    set_sops_label(new_label)
    return new_label


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
        question = f"AGE_KEY_FILE already exists at {AGE_KEY_FILE}. Overwrite?"
        existing_label = _device_label_of(get_current_device_public_key(), read_sops_yaml_keys())
        if existing_label:
            question = (
                f"AGE_KEY_FILE already exists at {AGE_KEY_FILE} and is registered as '{existing_label}'. "
                "Overwrite? An unregistered new key takes over that label and the old key is revoked."
            )
        if not prompts.confirm(question):
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
    replaced_label = None
    kept_label = None
    if register:
        # A new key replaces the registered key of this device: the old key
        # must lose access instead of staying an orphaned recipient. A key
        # that is already registered keeps its own label and leaves the old
        # one for the user to remove.
        known_keys = read_sops_yaml_keys()
        previous_label = _device_label_of(get_current_device_public_key(), known_keys)
        if previous_label and known_keys[previous_label] != pub:
            if pub in known_keys.values():
                kept_label = previous_label
            else:
                replaced_label = previous_label
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
        if replaced_label:
            # Revoking is complete only when every document is re-encrypted
            # without the old key, which needs the old key to open it first.
            for path, opened in ((SECRETS_FILE, previous_secrets), (RECOVERY_KEY_FILE, previous_recovery_private)):
                if path.exists() and opened is None:
                    staged_key.cleanup()
                    log.hint("Reseal it from an authorized device first, or remove that label there.")
                    raise RuntimeError(
                        f"cannot revoke the old key of '{replaced_label}': it cannot open {path.name}, "
                        "so that file cannot be re-encrypted without it"
                    )

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
    if replaced_label:
        current_label = _replace_device_key(replaced_label, pub, label)
    else:
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
    if replaced_label:
        log.warn("key", "previous device key revoked", label=replaced_label)
    if kept_label:
        log.warn("key", f"the previous key of this device is still registered as '{kept_label}'")
        log.hint(f"Run 'envy key rm {kept_label}' if no device uses that key any more.")

    # Warn if recovery public key exists but recovery-key.age is missing
    if "recovery" in keys and not RECOVERY_KEY_FILE.exists():
        log.warn("key", "recovery key is in .sops.yaml but secrets/recovery-key.age is missing")
        log.hint("Without recovery-key.age, you cannot export (envy key rr) the recovery private key from this device.")

    return pub
