"""Key lifecycle management for sops/age encryption — Typer subgroup for envy key."""

import os
import re
import subprocess
import sys
from functools import wraps
from pathlib import Path
from typing import List, Optional

import typer
from rich.prompt import Confirm
from rich.table import Table
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window, FormattedTextControl
from prompt_toolkit.styles import Style as PtStyle

from envy import log
from envy.utils import (
    ENVY_ROOT, HOME_DIR, AGE_KEY_DIR, AGE_KEY_FILE,
    SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE,
    DEVICE_LABEL_FILE, run_cmd, backup_sensitive_file,
    is_sops_encrypted,
)
from envy.git_safety import SecretSafetyError
from envy.secure_io import (
    atomic_write_text,
    ensure_private_directory,
)
from envy.transaction import FileTransaction, transaction_lock
from envy.git_commit import commit_staged_files, stage_repo_files
from envy.process import CommandError
from envy.keys.storage import (
    current_device_public_key as get_current_device_public_key,
    device_public_keys,
    generate_device_age_key,
    prepare_device_age_key,
    prepare_generated_device_age_key,
    commit_prepared_device_age_key,
    PreparedDeviceKey,
    read_sops_yaml_keys,
    store_device_age_key,
    write_sops_yaml_keys,
)
from envy.keys.identity import ensure_sops_label, get_sops_label, sanitize_label, set_sops_label
from envy.keys.recovery import (
    decrypt_recovery_key,
    generate_keypair,
    public_key_from_private as _public_key_from_private,
    reencrypt_recovery_key_with as _reencrypt_recovery_key_with,
    write_encrypted_recovery_key as _write_encrypted_recovery_key,
)


def prompt_input(message: str) -> str:
    """Read one interactive value without background output moving the cursor."""
    with log.interactive_prompt():
        return pt_prompt(message)


# Typer subgroup
app = typer.Typer(
    name="key",
    help="Manage age keys for sops encryption",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

# --yes/-y global flag
_yes_flag = False


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


def _set_yes_flag(yes: bool):
    global _yes_flag
    _yes_flag = yes

# ==========================================
# COMPLETION CALLBACKS
# ==========================================


def complete_sops_labels(ctx, incomplete):
    """Complete key labels from the parsed managed recipient list."""
    del ctx
    try:
        labels = read_sops_yaml_keys()
    except (OSError, ValueError):
        return []
    return [name for name in labels if name.startswith(incomplete)]


def complete_export_formats(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete the public key export formats supported by envY."""
    del ctx
    formats = {
        "age": "age public key format",
        "ssh": "SSH public key format",
    }
    return [
        (name, description)
        for name, description in formats.items()
        if name.startswith(incomplete.casefold())
    ]

def confirm(prompt_text: str) -> bool:
    """Require explicit y/n confirmation. Auto-answer yes with --yes/-y."""
    if _yes_flag:
        log.hint(f"{prompt_text} → auto-yes")
        return True
    return Confirm.ask(prompt_text, console=log.console)

def run_sops_updatekeys(*, age_key_file: Path | None = None) -> None:
    """Re-encrypt secrets with the current recipients, or fail closed."""
    if not SECRETS_FILE.exists():
        log.warn("key", "no secrets file to updatekeys")
        return
    if not is_sops_encrypted(SECRETS_FILE):
        raise SecretSafetyError("refusing updatekeys: secrets/secrets.yaml is not sops-encrypted")
    from envy.config import read_secrets_data, write_secrets_data
    data, decrypt_ok = read_secrets_data(age_key_file=age_key_file)
    if data is None or not decrypt_ok:
        raise SecretSafetyError("refusing updatekeys: cannot decrypt secrets/secrets.yaml")
    write_secrets_data(data)
    log.ok("key", "secrets re-encrypted with updated key list")


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


def _stage_repo_files(files: list[Path]) -> list[str]:
    """Stage explicit repository files and return changed relative pathspecs."""
    return stage_repo_files(files, repository=ENVY_ROOT)


def _commit_staged_files(changed: list[str], message: str) -> None:
    commit_staged_files(
        changed,
        message,
        confirm=confirm,
        repository=ENVY_ROOT,
    )


def git_commit_sops_files(operation: str = "") -> None:
    if not (ENVY_ROOT / ".git").exists():
        return

    label = get_sops_label()
    scope = f"sops/{operation}" if operation else "sops"

    changed = _stage_repo_files([SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE])

    if not changed:
        log.hint("sops files: no changes to commit")
        return

    names = [Path(f).name for f in changed]
    msg = f"chore({scope}): update keys on {label} ({', '.join(names)})"
    _commit_staged_files(changed, msg)


def _key_repo_snapshot() -> tuple[tuple[bool, bytes], ...]:
    return tuple(
        (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in (SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE)
    )


def _run_key_operation(operation: str, function, *args, **kwargs):
    """Commit only repository files changed by one successful key transaction."""
    before = _key_repo_snapshot()
    try:
        result = function(*args, **kwargs)
    except (OSError, RuntimeError, ValueError, SecretSafetyError, subprocess.SubprocessError) as exc:
        log.error("key", str(exc))
        raise typer.Exit(code=1) from exc
    if _key_repo_snapshot() != before:
        git_commit_sops_files(operation)
    return result


def git_commit_setup_files(machine_path: Path) -> None:
    """Offer one scoped commit for files managed by an envy setup save."""
    if not (ENVY_ROOT / ".git").exists():
        return

    changed = _stage_repo_files([
        machine_path,
        SOPS_YAML,
        SECRETS_FILE,
        RECOVERY_KEY_FILE,
    ])
    if not changed:
        log.hint("setup files: no changes to commit")
        return

    names = [Path(path).name for path in changed]
    message = (
        f"chore(setup): update {machine_path.stem} configuration "
        f"({', '.join(names)})"
    )
    _commit_staged_files(changed, message)

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
# INTERACTIVE MENU (prompt_toolkit)
# ==========================================


def _select_menu(items: list, title: str = "Select") -> Optional[tuple]:
    """Arrow-key navigation menu using prompt_toolkit Application.
    items: list of (fragments, action_type, data)
    Returns (action_type, data) on Enter, None on Esc/q."""

    class MenuState:
        cursor = 0
        result = None

    state = MenuState()

    def render():
        lines = [("class:title", f"  {title}\n")]
        for i, (fragments, action, data) in enumerate(items):
            base = "class:cursor" if i == state.cursor else "class:normal"
            prefix = "  ► " if i == state.cursor else "    "
            lines.append((base, prefix))
            for style_suffix, text in fragments:
                if style_suffix:
                    lines.append((f"class:{style_suffix}", text))
                else:
                    lines.append((base, text))
            lines.append((base, "\n"))
        lines.append(("class:bottom", "\n  Enter: confirm  │  Esc/q: cancel  │  ↑↓: navigate"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        if state.cursor > 0:
            state.cursor -= 1
        event.app.invalidate()

    @kb.add("down")
    def _(event):
        if state.cursor < len(items) - 1:
            state.cursor += 1
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        _, action, data = items[state.cursor]
        state.result = (action, data)
        event.app.exit()

    @kb.add("escape")
    @kb.add("q")
    def _(event):
        state.result = None
        event.app.exit()

    content = FormattedTextControl(render)
    layout = Layout(Window(content=content))
    style = PtStyle.from_dict({
        "title": "#ansicyan bold",
        "cursor": "bg:#ansicyan #ansiblack bold",
        "normal": "",
        "usb": "#ansiyellow bold",
        "bottom": "#ansigray",
    })

    app_instance = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    app_instance.run()
    return state.result

# ==========================================
# USB KEY SCANNING
# ==========================================


def scan_usb_key_files() -> List[Path]:
    """Scan mounted USB drives for .age/.age.txt files."""
    candidates = []
    mount_dirs = []

    if sys.platform == "darwin":
        mount_dirs = [Path("/Volumes")]
    else:
        mount_dirs = [Path(f"/media/{os.getenv('USER', '')}"), Path("/mnt")]

    for mount_dir in mount_dirs:
        if not mount_dir.exists():
            continue
        for volume in mount_dir.iterdir():
            if not volume.is_dir():
                continue

            recovery_dir = volume / "recovery"
            if recovery_dir.exists() and recovery_dir.is_dir():
                for f in sorted(recovery_dir.iterdir()):
                    if f.is_file() and (f.name.endswith(".age") or f.name.endswith(".age.txt")):
                        candidates.append(f)

            for f in sorted(volume.iterdir()):
                if f.is_file() and (f.name.endswith(".age") or f.name.endswith(".age.txt")):
                    candidates.append(f)

    return candidates

# ==========================================
# KEY SUBCOMMANDS
# ==========================================


def key_list() -> None:
    keys = read_sops_yaml_keys()
    current_pub = get_current_device_public_key()
    current_label = get_sops_label()

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
        run_sops_updatekeys()
        run_cmd(["sops", "--decrypt", str(SECRETS_FILE)], capture=True)
    log.ok("key", "key repository repaired and verified")


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
        if not force and not confirm("This is your CURRENT device key! Removing it will make secrets inaccessible here. Remove anyway?"):
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


@_transactional_key_files
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
        if not confirm(f"AGE_KEY_FILE already exists at {AGE_KEY_FILE}. Overwrite?"):
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
        usb_files = scan_usb_key_files()

        items = []
        for f in usb_files:
            tag_text = "USB " if f.parent.name == "recovery" else "USB "
            path_text = str(f)
            items.append(([("usb", tag_text), ("", path_text)], "usb", str(f)))
        items.append(([("", "Import age key file (specify path)")], "age_manual", ""))
        items.append(([("", "Import SSH key and derive age key")], "ssh_manual", ""))
        items.append(([("", "Generate new age key")], "generate", ""))
        items.append(([("", "Quit")], "quit", ""))

        result = _select_menu(items, title="Import age key for this device")
        if result is None or result[0] == "quit":
            return None

        action, data = result
        if action == "usb":
            return key_import(age_path=data, label=label, register=register, prepared=prepared)
        elif action == "age_manual":
            default_path = "~/Library/Application Support/sops/age/keys.txt"
            try:
                path = prompt_input(f"Path to age key file [{default_path}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not path:
                path = default_path
            return key_import(age_path=path, label=label, register=register, prepared=prepared)
        elif action == "ssh_manual":
            default_ssh = "~/.ssh/id_ed25519"
            try:
                path = prompt_input(f"Path to SSH private key [{default_ssh}]: ").strip()
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
            from envy.config import read_secrets_data
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
    current_label = register_device_key(pub, label)
    keys = read_sops_yaml_keys()
    if previous_recovery_private is not None:
        from envy.keys.recovery import write_encrypted_recovery_key
        write_encrypted_recovery_key(previous_recovery_private, keys.values())
    elif RECOVERY_KEY_FILE.exists():
        try:
            decrypt_recovery_key()
            reencrypt_recovery_key(keys)
        except (CommandError, OSError, RuntimeError, ValueError):
            log.warn("key", "registered device cannot decrypt existing recovery-key.age; left it unchanged")
    if previous_secrets is not None:
        from envy.config import write_secrets_data
        write_secrets_data(previous_secrets)
    elif SECRETS_FILE.exists():
        from envy.config import read_secrets_data
        _, secrets_accessible = read_secrets_data()
        if secrets_accessible:
            run_sops_updatekeys()
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
            if confirm("Seal recovery key into recovery-key.age?"):
                key_seal_recovery()
            else:
                log.hint("Run: envy key seal-recovery")

        if confirm("Rotate device key now?"):
            key_rotate()
            new_pub = get_current_device_public_key()
            log.ok("key", "device key rotated", pubkey=new_pub)
            return new_pub

    return pub


@_transactional_key_files
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
        run_sops_updatekeys()

    log.ok("key", "recovery key generated and added to .sops.yaml")
    log.hint(f"Recovery public key: {recovery_pub[:30]}...")
    log.info("key", "recovery private key is stored encrypted at secrets/recovery-key.age")
    log.warn("key", "IMPORTANT: also save the recovery key offline (USB/paper/password manager) as ultimate backup")


@_transactional_key_files
def key_seal_recovery(priv_path: Optional[str] = None) -> None:
    """Encrypt a recovery private key into secrets/recovery-key.age."""
    keys = read_sops_yaml_keys()
    if "recovery" not in keys:
        raise RuntimeError("no recovery key in .sops.yaml; run: envy key add-recovery")

    if RECOVERY_KEY_FILE.exists():
        log.warn("key", "recovery-key.age already exists, re-encrypting with updated key list")
        reencrypt_recovery_key(keys)
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
        run_sops_updatekeys()
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


@_transactional_key_files
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
        run_sops_updatekeys()

        recipients = [v for k, v in keys.items() if k not in {"recovery", "recovery_new"}]
        _write_encrypted_recovery_key(priv_line, recipients)

        keys.pop("recovery")
        keys["recovery"] = new_recovery_pub
        keys.pop("recovery_new")
        write_sops_yaml_keys(keys)
        run_sops_updatekeys()
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
    run_sops_updatekeys()

    # Remove the old device recipient before publishing the final recovery
    # ciphertext. A retired device must not retain access through recovery.
    final_keys = dict(keys)
    final_keys.pop(old_label_temp)
    _reencrypt_recovery_key_with(final_keys)

    keys.pop(old_label_temp)
    write_sops_yaml_keys(keys)
    run_sops_updatekeys()

    backup_sensitive_file(AGE_KEY_FILE)
    store_device_age_key(new_key_content)

    log.ok("key", f"key rotation complete for '{label}'")
    log.hint(f"New public key: {new_pub}")


# ==========================================
# TYPER COMMAND REGISTRATION
# ==========================================

# Global -y/--yes flag handler
@app.callback()
def key_callback(
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-answer yes to all confirmations"),
):
    """Manage age keys for sops encryption."""
    _set_yes_flag(yes)


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list():
    """Show keys in .sops.yaml + current device status."""
    key_list()


@app.command(name="status")
@app.command(name="st", rich_help_panel="Aliases")
def cmd_status():
    """Check key status and decryptability."""
    key_status()


@app.command(name="repair")
def cmd_repair():
    """Repair interrupted rotations and revalidate encrypted key material."""
    _run_key_operation("repair", key_repair)


@app.command(name="add")
@app.command(name="a", rich_help_panel="Aliases")
def cmd_add(
    pubkey: str = typer.Argument(help="Age public key to add"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Label for this key"),
):
    """Add a device key to .sops.yaml."""
    _run_key_operation("add", key_add, pubkey, label)


@app.command(name="remove")
@app.command(name="rm", rich_help_panel="Aliases")
def cmd_remove(
    label: str = typer.Argument(help="Label of key to remove", autocompletion=complete_sops_labels),
    force: bool = typer.Option(False, "--force", "-f", help="Allow removing current device key"),
):
    """Remove a device key from .sops.yaml."""
    _run_key_operation("remove", key_remove, label, force)


@app.command(name="export")
@app.command(name="ex", rich_help_panel="Aliases")
def cmd_export(
    format: str = typer.Option(
        "age", "--format", "-F", help="Export format (age, ssh)",
        autocompletion=complete_export_formats,
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Export current device key for transfer."""
    key_export(format, output)


@app.command(name="rotate")
def cmd_rotate(
    recovery: bool = typer.Option(False, "--recovery", "-r", help="Rotate recovery key instead"),
):
    """Rotate current device age key."""
    _run_key_operation("rotate_recovery" if recovery else "rotate", key_rotate, recovery)


@app.command(name="import")
@app.command(name="im", rich_help_panel="Aliases")
def cmd_import(
    age: Optional[str] = typer.Option(None, "--age", "-a", help="Path to age key file"),
    ssh: Optional[str] = typer.Option(None, "--ssh", "-s", help="Path to SSH private key"),
    generate: bool = typer.Option(False, "--generate", "-g", help="Generate a new age key"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Sops key label"),
):
    """Import a key for current device."""
    result = _run_key_operation(
        "import",
        key_import,
        age_path=age,
        ssh_path=ssh,
        generate=generate,
        label=label,
    )
    if result is None:
        # Explicit failures are errors; an interactive quit is a cancellation.
        raise typer.Exit(code=2 if not age and not ssh and not generate else 1)


@app.command(name="add-recovery")
@app.command(name="ar", rich_help_panel="Aliases")
def cmd_add_recovery():
    """Generate a recovery key."""
    _run_key_operation("add_recovery", key_add_recovery)


@app.command(name="seal-recovery")
@app.command(name="sr", rich_help_panel="Aliases")
def cmd_seal_recovery(
    priv_path: Optional[str] = typer.Argument(None, help="Path to recovery private key file"),
):
    """Encrypt recovery private key into recovery-key.age."""
    _run_key_operation("seal_recovery", key_seal_recovery, priv_path)


@app.command(name="recover-recovery")
@app.command(name="rr", rich_help_panel="Aliases")
def cmd_recover_recovery(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save decrypted recovery key to file"),
):
    """Decrypt stored recovery key."""
    key_recover_recovery(output)
