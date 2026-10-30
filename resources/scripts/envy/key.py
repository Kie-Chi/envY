"""Key lifecycle management for sops/age encryption — Typer subgroup for envy key."""

from typing import Optional

import typer

from envy.keys.storage import read_sops_yaml_keys
from envy.keys import commits, importing, operations, prompts, rotation


# Typer subgroup
app = typer.Typer(
    name="key",
    help="Manage age keys for sops encryption",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


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


# ==========================================
# TYPER COMMAND REGISTRATION
# ==========================================

# Global -y/--yes flag handler
@app.callback()
def key_callback(
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-answer yes to all confirmations"),
):
    """Manage age keys for sops encryption."""
    prompts._set_yes_flag(yes)


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list():
    """Show keys in .sops.yaml + current device status."""
    operations.key_list()


@app.command(name="status")
@app.command(name="st", rich_help_panel="Aliases")
def cmd_status():
    """Check key status and decryptability."""
    operations.key_status()


@app.command(name="repair")
def cmd_repair():
    """Repair interrupted rotations and revalidate encrypted key material."""
    commits._run_key_operation("repair", rotation.key_repair)


@app.command(name="add")
@app.command(name="a", rich_help_panel="Aliases")
def cmd_add(
    pubkey: str = typer.Argument(help="Age public key to add"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Label for this key"),
):
    """Add a device key to .sops.yaml."""
    commits._run_key_operation("add", operations.key_add, pubkey, label)


@app.command(name="remove")
@app.command(name="rm", rich_help_panel="Aliases")
def cmd_remove(
    label: str = typer.Argument(help="Label of key to remove", autocompletion=complete_sops_labels),
    force: bool = typer.Option(False, "--force", "-f", help="Allow removing current device key"),
):
    """Remove a device key from .sops.yaml."""
    commits._run_key_operation("remove", operations.key_remove, label, force)


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
    operations.key_export(format, output)


@app.command(name="rotate")
def cmd_rotate(
    recovery: bool = typer.Option(False, "--recovery", "-r", help="Rotate recovery key instead"),
):
    """Rotate current device age key."""
    commits._run_key_operation("rotate_recovery" if recovery else "rotate", rotation.key_rotate, recovery)


@app.command(name="import")
@app.command(name="im", rich_help_panel="Aliases")
def cmd_import(
    age: Optional[str] = typer.Option(None, "--age", "-a", help="Path to age key file"),
    ssh: Optional[str] = typer.Option(None, "--ssh", "-s", help="Path to SSH private key"),
    generate: bool = typer.Option(False, "--generate", "-g", help="Generate a new age key"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Sops key label"),
):
    """Import a key for current device."""
    result = commits._run_key_operation(
        "import",
        importing.key_import,
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
    commits._run_key_operation("add_recovery", rotation.key_add_recovery)


@app.command(name="seal-recovery")
@app.command(name="sr", rich_help_panel="Aliases")
def cmd_seal_recovery(
    priv_path: Optional[str] = typer.Argument(None, help="Path to recovery private key file"),
):
    """Encrypt recovery private key into recovery-key.age."""
    commits._run_key_operation("seal_recovery", rotation.key_seal_recovery, priv_path)


@app.command(name="recover-recovery")
@app.command(name="rr", rich_help_panel="Aliases")
def cmd_recover_recovery(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save decrypted recovery key to file"),
):
    """Decrypt stored recovery key."""
    rotation.key_recover_recovery(output)
