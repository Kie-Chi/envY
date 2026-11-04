"""The envy config command group."""

import json
import os
import sys
from getpass import getpass

import typer
from rich.table import Table
from rich.text import Text

from envy import log
from envy.evaluation import machine_manifest, manifest_settings
from envy.jsonio import emit, emit_error
from envy.schemas.config import MACHINE_FIELDS, SECRET_FIELDS
from envy.process import run_process
from envy.mutation import offer_mutation_commit
from envy.utils import (
    SOPS_YAML,
    SECRETS_FILE,
    current_machine_id,
    platform_name,
    read_device_metadata,
)
from envy.config import machine_nix, refine, secret_store, updates


app = typer.Typer(
    name="config",
    help="Validate device metadata and edit the selected machine configuration and sops secrets",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def complete_machine_paths(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete managed non-secret machine option paths."""
    from envy.agent_completion import complete_agent_paths

    return [
        (field.path, field.prompt or field.group)
        for field in MACHINE_FIELDS
        if field.path.startswith(incomplete)
    ] + complete_agent_paths(ctx, incomplete)


def complete_secret_paths(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete secret YAML paths without reading or exposing secret values."""
    from envy.agent_completion import complete_credential_paths

    candidates = dict(complete_credential_paths(ctx, incomplete))
    candidates.update({
        field.yaml_path: field.prompt or field.group
        for field in SECRET_FIELDS
        if field.yaml_path and field.yaml_path.startswith(incomplete)
    })
    return sorted(candidates.items())


def complete_machine_values(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete enumerated values for the machine path already selected."""
    params = getattr(ctx, "params", {}) if ctx is not None else {}
    path = params.get("path") if isinstance(params, dict) else None
    field = next((item for item in MACHINE_FIELDS if item.path == path), None)
    if field is None:
        from envy.agent_completion import complete_agent_values

        return complete_agent_values(ctx, incomplete)
    if not field.choices:
        return []
    return [
        (choice, field.path)
        for choice in field.choices
        if choice.startswith(incomplete)
    ]


@app.command(name="check")
def cmd_check():
    """Strictly check all managed configuration without writing."""
    report = refine.refine_all(write=False, strict=True, include_secrets=True)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command(name="refine")
def cmd_refine(
    prune: bool = typer.Option(False, "--prune", help="Remove known obsolete config/secret paths"),
):
    """Refine device metadata, the selected machine file, and secrets.yaml."""
    report = refine.refine_all(write=True, strict=True, include_secrets=True, prune=prune)
    if not report.ok:
        raise typer.Exit(code=1)
    if report.changed:
        offer_mutation_commit(
            [machine_nix.machine_config_file(), SECRETS_FILE],
            f"chore(config): refine {current_machine_id()}",
        )


@app.command(name="doctor")
def cmd_doctor():
    """Run a stricter local config and secrets diagnostic."""
    report = refine.refine_all(write=False, strict=True, include_secrets=True)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command(name="set")
def cmd_set(
    path: str = typer.Argument(
        ..., help="Machine or agent path, for example envy.vscode.mode or envy.agents.pi.api",
        autocompletion=complete_machine_paths,
    ),
    value: str = typer.Argument(..., help="Value to write", autocompletion=complete_machine_values),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Assume yes; skip interactive commit prompts"
    ),
):
    """Set a managed non-secret value in the selected machine file."""
    try:
        updates.set_config_value(path, value, quiet=json_output)
    except typer.BadParameter as exc:
        if json_output:
            emit_error("config.set", str(exc), code="invalid-value")
            raise typer.Exit(code=1) from exc
        raise
    offer_mutation_commit(
        [machine_nix.machine_config_file()],
        f"chore(config): update {current_machine_id()} machine values",
        quiet=json_output or yes,
    )
    if json_output:
        emit("config.set", data={
            "path": path,
            "value": value,
            "machine": current_machine_id(),
        })


@app.command(name="secret-set")
def cmd_secret_set(
    yaml_path: str = typer.Argument(
        ..., help="Secret path, for example llm/steps/apikey", autocompletion=complete_secret_paths,
    ),
    stdin: bool = typer.Option(False, "--stdin", help="Read the secret from standard input"),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Initialize a new secrets file without reading the existing ciphertext",
    ),
):
    """Set a secret without exposing it in argv or shell history."""
    if stdin:
        value = sys.stdin.read().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            log.error("secrets", "non-interactive secret input requires --stdin")
            raise typer.Exit(code=1)
        value = getpass(f"{yaml_path}: ")
    try:
        updates.set_secret_value(yaml_path, value, replace=replace)
    except (OSError, RuntimeError, ValueError) as exc:
        log.error("secrets", str(exc))
        if replace:
            log.hint("The new secrets file was not written; retry with a valid age installation.")
        else:
            log.hint("Use --replace only when this is a new device and the old secrets are intentionally discarded.")
        raise typer.Exit(code=1) from exc
    offer_mutation_commit(
        [SOPS_YAML, SECRETS_FILE],
        f"chore(secrets): {'replace' if replace else 'update'} {yaml_path}",
    )


@app.command(name="show")
def cmd_show(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show evaluated machine values and secret status."""
    manifest = machine_manifest(refresh=refresh)
    evaluated_values = manifest_settings(manifest)
    config_values = evaluated_values or machine_nix.read_machine_nix()
    device_values = read_device_metadata()
    secret_values, _ = secret_store.read_secrets_yaml()
    secret_data, decrypt_ok = secret_store.read_secrets_data()
    from envy.agent import config_view

    agent_view = config_view(manifest, secret_data=secret_data, decrypt_ok=decrypt_ok)
    dynamic_values = agent_view["values"]
    dynamic_fields = agent_view["fields"]
    manifest_llm = agent_view["llm"]
    manifest_agents = agent_view["agents"]
    secret_values_for_fields: dict[str, str] = {}
    for secret_field in SECRET_FIELDS:
        secret_values_for_fields[secret_field.path] = str(secret_values.get(secret_field.path, ""))
    payload = {
        "schemaVersion": 1,
        "source": "evaluated" if evaluated_values else "source-fallback",
        # Keep the target platform in the machine-readable config envelope.
        # It is runtime metadata, not a user-editable machine option.
        "platform": platform_name(),
        "device": {
            "machineId": device_values.get("machine_id", ""),
            "sopsLabel": device_values.get("sops_label", ""),
        },
        "values": {
            field.path: config_values.get(field.path, "")
            for field in MACHINE_FIELDS
        } | dynamic_values,
        "fields": [
            {
                "path": field.path,
                "value": config_values.get(field.path, ""),
                "choices": list(field.choices),
            }
            for field in MACHINE_FIELDS
        ] + dynamic_fields,
        "llm": manifest_llm,
        "agents": manifest_agents,
        "secrets": {
            field.yaml_path: bool(secret_values_for_fields.get(field.path, ""))
            for field in SECRET_FIELDS
        },
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="envy config (evaluated)" if evaluated_values else "envy config (source fallback)")
    table.add_column("Kind")
    table.add_column("Path")
    table.add_column("Value")
    table.add_row("device", "device.machine_id", Text(device_values.get("machine_id", "")))
    table.add_row("device", "device.sops_label", Text(device_values.get("sops_label", "")))
    for f in MACHINE_FIELDS:
        table.add_row("value", Text(f.path), Text(str(config_values.get(f.path, ""))))
    for dynamic_field in dynamic_fields:
        path = dynamic_field["path"]
        if path.startswith("envy.llm.endpoints."):
            kind = "endpoint"
        elif path.startswith("envy.llm.credentials."):
            kind = "credential"
        else:
            kind = "agent"
        table.add_row(kind, Text(path), Text(str(dynamic_field["value"])))
    credentials = manifest_llm.get("credentials", {}) if isinstance(manifest_llm, dict) else {}
    if isinstance(credentials, dict):
        for name, credential in sorted(credentials.items()):
            if isinstance(credential, dict):
                table.add_row(
                    "secret",
                    Text(f"credential/{name}"),
                    "<set>" if credential.get("credentialSet") else "<empty>",
                )
    if not manifest:
        log.warn(
            "config",
            "Nix evaluation failed; showing direct machine assignments without imported defaults",
        )

    for f in SECRET_FIELDS:
        value = secret_values_for_fields[f.path]
        status = "<set>" if value else "<empty>"
        table.add_row("secret", Text(f.yaml_path), status)

    log.console.print(table)


@app.command(name="edit")
def cmd_edit():
    """Open the selected versioned machine file in $EDITOR."""
    path = machine_nix.machine_config_file()
    if not path.exists():
        log.error("machine", "selected machine configuration is missing", path=str(path))
        raise typer.Exit(code=1)
    editor = os.environ.get("EDITOR", "vim")
    run_process([editor, str(path)], check=True)
