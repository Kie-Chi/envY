"""Key-file transactions, recipient registration, updates, reports, and export."""

import re
import subprocess
from functools import wraps
from pathlib import Path
from typing import Optional

from rich.table import Table

from envy import log
from envy.utils import (
    ENVY_ROOT,
    HOME_DIR,
    AGE_KEY_FILE,
    SOPS_YAML,
    SECRETS_FILE,
    RECOVERY_KEY_FILE,
    DEVICE_LABEL_FILE,
    run_cmd,
    is_sops_encrypted,
)
from envy.git_safety import SecretSafetyError
from envy.secure_io import atomic_write_text
from envy.transaction import FileTransaction, transaction_lock
from envy.keys.storage import (
    current_device_public_key as get_current_device_public_key,
    read_sops_yaml_keys,
    write_sops_yaml_keys,
)
from envy.keys.identity import ensure_sops_label, get_sops_label, sanitize_label, set_sops_label
from envy.keys.recovery import reencrypt_recovery_key_with as _reencrypt_recovery_key_with
from envy.keys import prompts


def _transactional_key_files(function):
    """Roll back every key-owned file when a lifecycle operation raises."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        paths = [
            SOPS_YAML,
            SECRETS_FILE,
            RECOVERY_KEY_FILE,
            AGE_KEY_FILE,
            DEVICE_LABEL_FILE,
        ]
        with transaction_lock(ENVY_ROOT / ".envy-key.lock"):
            with FileTransaction(paths) as transaction:
                result = function(*args, **kwargs)
                transaction.commit()
            return result

    return wrapped


def run_sops_updatekeys(*, age_key_file: Path | None = None) -> None:
    """Re-encrypt secrets with the current recipients, or fail closed."""
    if not SECRETS_FILE.exists():
        log.warn("key", "no secrets file to updatekeys")
        return
    if not is_sops_encrypted(SECRETS_FILE):
        raise SecretSafetyError("refusing updatekeys: secrets/secrets.yaml is not sops-encrypted")
    from envy.config.secret_store import read_secrets_data, write_secrets_data
    data, decrypt_ok = read_secrets_data(age_key_file=age_key_file)
    if data is None or not decrypt_ok:
        raise SecretSafetyError("refusing updatekeys: cannot decrypt secrets/secrets.yaml")
    write_secrets_data(data)
    log.ok("key", "secrets re-encrypted with updated key list")


def reencrypt_recovery_key(keys: dict[str, str]) -> None:
    """Re-encrypt recovery-key.age with all current device keys from .sops.yaml."""
    _reencrypt_recovery_key_with(keys)


def register_device_key(public_key: str, label: str | None = None) -> str:
    """Register a device recipient without requiring access to old ciphertext."""
    keys = read_sops_yaml_keys()
    matching = [
        name for name, value in keys.items()
        if value == public_key and name != "recovery" and not name.startswith("envy_rotation_old_")
    ]
    if matching:
        if len(matching) != 1:
            raise RuntimeError("device public key is registered under multiple labels")
        set_sops_label(matching[0])
        return matching[0]

    if label:
        resolved = sanitize_label(label)
        if not resolved:
            raise ValueError("device sops label cannot be empty")
    else:
        try:
            resolved = ensure_sops_label(public_key=public_key)
        except ValueError:
            # A stale ignored label must not select or overwrite another
            # device. Derive a fresh label from the current host instead.
            resolved = sanitize_label(run_cmd(["hostname", "-s"], check=False, capture=True) or "device")
            suffix = 2
            original = resolved
            while resolved in keys and keys[resolved] != public_key:
                resolved = f"{original}_{suffix}"
                suffix += 1
    if resolved in {"recovery", "recovery_new"} or resolved.startswith("envy_rotation_old_"):
        raise ValueError("device sops label is reserved")
    if resolved in keys and keys[resolved] != public_key:
        raise ValueError(f"sops key label '{resolved}' belongs to another age key")
    keys[resolved] = public_key
    write_sops_yaml_keys(keys)
    set_sops_label(resolved)
    return resolved


# ==========================================
# KEY SUBCOMMANDS
# ==========================================


def key_list() -> None:
    keys = read_sops_yaml_keys()
    current_pub = get_current_device_public_key()

    table = Table(title="Age Keys in .sops.yaml")
    table.add_column("Label", style="cyan")
    table.add_column("Public Key", style="white")
    table.add_column("Status")

    for label, pubkey in keys.items():
        is_current = pubkey == current_pub
        if label == "recovery":
            status = "[magenta]RECOVERY[/magenta]"
        elif is_current:
            status = "[green]THIS DEVICE[/green]"
        else:
            status = "[dim]remote[/dim]"
        table.add_row(label, pubkey[:24] + "...", status)

    log.console.print(table)

    if current_pub and current_pub in keys.values():
        log.ok("key", "current device CAN decrypt secrets")
    elif current_pub:
        log.error("key", "current device key NOT in .sops.yaml — cannot decrypt")
        log.hint("Run: envy key import or envy key add")
    else:
        log.error("key", "no age key on this device")
        log.hint("Run: envy key import")


def key_status() -> None:
    current_pub = get_current_device_public_key()
    keys = read_sops_yaml_keys()
    label = get_sops_label()

    items = []

    if AGE_KEY_FILE.exists():
        items.append(("Age key file", "[green]PRESENT[/green]", str(AGE_KEY_FILE)))
    else:
        items.append(("Age key file", "[red]MISSING[/red]", str(AGE_KEY_FILE)))

    if current_pub:
        items.append(("Public key", f"[green]{current_pub[:30]}...[/green]", ""))
        in_sops = current_pub in keys.values()
        items.append(("In .sops.yaml", "[green]YES[/green]" if in_sops else "[red]NO[/red]", ""))
    else:
        items.append(("Public key", "[red]NONE[/red]", ""))
        items.append(("In .sops.yaml", "[red]NO KEY[/red]", ""))

    items.append(("Sops key label", label, str(DEVICE_LABEL_FILE)))

    has_recovery = "recovery" in keys
    items.append(("Recovery key", "[green]YES[/green]" if has_recovery else "[yellow]NO[/yellow]", ""))

    can_decrypt = False
    if SECRETS_FILE.exists() and current_pub and current_pub in keys.values():
        try:
            run_cmd(["sops", "--decrypt", str(SECRETS_FILE)], check=True, capture=True)
            can_decrypt = True
        except subprocess.CalledProcessError:
            can_decrypt = False
    status_str = "[green]YES[/green]" if can_decrypt else "[red]NO[/red]" if SECRETS_FILE.exists() else "[dim]N/A[/dim]"
    items.append(("Can decrypt", status_str, ""))

    items.append(("Total keys", str(len(keys)), ""))

    table = Table(title="Key Status")
    table.add_column("Property")
    table.add_column("Value")
    table.add_column("Detail")
    for prop, val, detail in items:
        table.add_row(prop, val, detail)
    log.console.print(table)


@_transactional_key_files
def key_add(pubkey: str, label: Optional[str] = None) -> None:
    if not re.fullmatch(r"age1[0-9a-z]+", pubkey):
        raise ValueError("invalid age public key")

    keys = read_sops_yaml_keys()
    if pubkey in keys.values():
        log.warn("key", "key already exists in .sops.yaml")
        return

    if label is None:
        label = f"device_{len(keys)}"
    label = sanitize_label(label)
    if label in {"recovery", "recovery_new"} or label.startswith("envy_rotation_old_"):
        raise ValueError(f"label '{label}' is reserved")

    if label in keys:
        raise ValueError(f"label '{label}' already used, choose a different label")

    try:
        run_cmd(["age", "--encrypt", "-r", pubkey, "-o", "/dev/null"], stdin_data="test", capture=True)
    except subprocess.CalledProcessError:
        raise ValueError("key rejected by age — not a valid recipient")

    new_keys = dict(keys)
    new_keys[label] = pubkey

    if RECOVERY_KEY_FILE.exists():
        _reencrypt_recovery_key_with(new_keys)

    write_sops_yaml_keys(new_keys)

    if SECRETS_FILE.exists():
        run_sops_updatekeys()

    log.ok("key", "key added", label=label)


@_transactional_key_files
def key_remove(label: str, force: bool = False) -> None:
    keys = read_sops_yaml_keys()
    current_pub = get_current_device_public_key()

    if label == "recovery_new" or label.startswith("envy_rotation_old_"):
        raise ValueError("cannot remove a rotation marker directly")
    if label not in keys:
        raise ValueError(f"label '{label}' not found in .sops.yaml")

    if label == "recovery":
        raise ValueError("cannot remove the recovery key — it ensures decryptability during rotation")

    if keys[label] == current_pub:
        if not force and not prompts.confirm("This is your CURRENT device key! Removing it will make secrets inaccessible here. Remove anyway?"):
            log.warn("key", "skipped")
            return

    remaining = len(keys) - 1
    if remaining < 2:
        raise RuntimeError(
            "cannot remove — at least one device key and the recovery key must remain"
        )

    removed_pub = keys.pop(label)
    write_sops_yaml_keys(keys)

    reencrypt_recovery_key(keys)

    run_sops_updatekeys()
    log.ok("key", "key removed", label=label, pubkey=removed_pub[:24] + "...")
    log.warn("key", "that device will NO LONGER be able to decrypt secrets")

    if removed_pub == current_pub:
        log.error("key", "WARNING: you removed your own key! Secrets inaccessible on this device until you import a new key")


def key_export(format: str = "age", output: Optional[str] = None) -> None:
    if not AGE_KEY_FILE.exists():
        raise RuntimeError("no age key on this device to export")
    if format not in {"age", "ssh"}:
        raise ValueError(f"unsupported key export format: {format}")

    content = AGE_KEY_FILE.read_text().strip()
    pub = get_current_device_public_key()

    if format == "age":
        if output:
            atomic_write_text(Path(output), content + "\n", mode=0o600)
            log.ok("key", "age key exported", path=output)
        else:
            print(content)
    elif format == "ssh":
        ssh_key = HOME_DIR / ".ssh" / "id_ed25519"
        if ssh_key.exists():
            if output:
                atomic_write_text(Path(output), ssh_key.read_text(), mode=0o600)
                log.ok("key", "SSH key exported", path=output)
            else:
                print(ssh_key.read_text().strip())
        else:
            log.warn("key", "no SSH key found, export as age format instead")
            return

    log.hint(f"Public key: {pub}")
    log.warn("key", "private keys are sensitive — transfer securely (USB, scp, encrypted channel)")
