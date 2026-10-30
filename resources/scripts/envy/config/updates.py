"""Validated single-value updates of machine config and secrets."""

import typer

from envy import log
from envy.schemas.config import MACHINE_FIELDS
from envy.transaction import FileTransaction, transaction_lock
from envy.utils import (
    AGE_KEY_FILE,
    DEVICE_LABEL_FILE,
    ENVY_ROOT,
    SOPS_YAML,
    SECRETS_FILE,
    current_machine_id,
)
from envy.config import machine_nix, refine, secret_store


def set_config_value(path: str, value: str, *, quiet: bool = False) -> None:
    if path.startswith(("envy.agents.", "envy.llm.endpoints.", "envy.llm.credentials.")):
        from envy.agent import _set
        try:
            _set(path, value)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not quiet:
            log.ok("machine", "agent value updated", field=path, machine=current_machine_id())
        return
    values = machine_nix.read_machine_nix()
    valid_paths = {f.path for f in MACHINE_FIELDS}
    if path not in valid_paths:
        if not quiet:
            log.error("machine", "unknown managed machine field", field=path)
            log.hint("Edit the machine file directly for custom policy.")
        raise typer.BadParameter(f"unknown managed machine field: {path}")
    for machine_field in MACHINE_FIELDS:
        if machine_field.path not in values:
            values[machine_field.path] = machine_field.default_fn()
    values[path] = value
    field_def = next(field for field in MACHINE_FIELDS if field.path == path)
    validation = refine.RefineReport()
    refine._validate_field(field_def, value, validation, "machine")
    if validation.errors:
        raise typer.BadParameter(f"invalid value for {path}")
    machine_nix.write_machine_nix(values)
    if not quiet:
        log.ok("machine", "machine value updated", field=path, machine=current_machine_id())


def set_secret_value(yaml_path: str, value: str, *, replace: bool = False) -> None:
    if not replace:
        secret_store.assert_no_plaintext_secret_artifacts()
        data, decrypt_ok = secret_store.read_secrets_data()
        if data is None or not decrypt_ok:
            raise RuntimeError(
                "cannot update a secret without decrypting secrets.yaml; "
                "use --replace on a new device to initialize a new secrets file"
            )
        secret_store._set_nested(data, yaml_path, value)
        secret_store.write_secrets_data(data)
        log.ok("secrets", "secret value updated", key=yaml_path)
        return

    from envy.keys.identity import ensure_sops_label
    from envy.keys.storage import (
        current_device_public_key,
        generate_device_age_key,
        read_sops_yaml_keys,
        write_sops_yaml_keys,
    )

    secret_store.assert_no_plaintext_secret_artifacts()
    with transaction_lock(ENVY_ROOT / ".envy-key.lock"):
        with FileTransaction([SOPS_YAML, SECRETS_FILE, AGE_KEY_FILE, DEVICE_LABEL_FILE]) as transaction:
            if not AGE_KEY_FILE.exists():
                generate_device_age_key()
            public_key = current_device_public_key()
            if not public_key:
                raise RuntimeError("cannot initialize secrets: current age key is unavailable")
            keys = read_sops_yaml_keys()
            label = ensure_sops_label(public_key=public_key)
            if label == "recovery":
                raise RuntimeError("device sops label cannot be recovery")
            keys[label] = public_key
            data: dict = {}
            secret_store._set_nested(data, yaml_path, value)
            write_sops_yaml_keys(keys)
            secret_store.write_secrets_data(data)
            transaction.commit()
    log.ok("secrets", "secret value replaced", key=yaml_path)
