#!/usr/bin/env python3
"""envY setup CLI for the selected machine file and sops secrets."""

import subprocess
from dataclasses import dataclass
from typing import Optional

from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window, HSplit, FormattedTextControl, ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import Style as PtStyle
from prompt_toolkit.widgets import Frame
from prompt_toolkit import prompt as pt_prompt

from envy import log
from envy.key import (
    AGE_KEY_DIR, AGE_KEY_FILE, SECRETS_FILE, SOPS_YAML, RECOVERY_KEY_FILE,
    read_sops_yaml_keys, write_sops_yaml_keys,
    get_current_device_public_key, ensure_sops_label,
    run_sops_updatekeys, git_commit_setup_files, key_import,
    generate_device_age_key, store_device_age_key,
    warn_if_device_key_is_recovery,
)
from envy.config import (
    read_machine_nix, read_secrets_yaml, write_machine_nix, write_secrets_yaml,
    RefineReport, _validate_field,
)
from envy.evaluation import machine_manifest, manifest_settings
from envy.host import initialize_machine, machine_file, validate_machine_id
from envy.schemas.config import ALL_FIELDS, MACHINE_FIELDS, SECRET_FIELDS, FieldDef
from envy.software import (
    ConcurrentMachineEdit,
    SoftwarePolicyError,
    build_software_items,
    groups_for_manifest,
    normalize_exclusions,
    read_managed_exclusions,
    set_excluded,
    software_changes,
    source_digest,
    write_and_validate_exclusions,
)
from envy.utils import (
    DEVICE_LABEL_FILE,
    HOME_DIR,
    PLATFORM,
    backup_sensitive_file,
    current_machine_id,
    run_cmd,
    set_device_machine_id,
)
from envy.secure_io import ensure_private_directory
from envy.transaction import FileTransaction
from envy.process import CommandError, render_command_error


# ==========================================
# CRYPTO — age key + sops (delegated to key.py)
# ==========================================


def setup_age_key() -> str:
    """Ensure the current device has an age key. Detects SSH rotation mismatches."""
    ensure_private_directory(AGE_KEY_DIR)

    if AGE_KEY_FILE.exists():
        current_pub = get_current_device_public_key()
        if current_pub:
            keys = read_sops_yaml_keys()
            if current_pub in keys.values():
                warn_if_device_key_is_recovery(current_pub, keys)
                return current_pub

            # Key exists but not in .sops.yaml — detect SSH rotation
            ssh_key = HOME_DIR / ".ssh" / "id_ed25519"
            if ssh_key.exists():
                try:
                    ssh_pub_input = ssh_key.with_suffix(".pub").read_text()
                    ssh_derived_pub = run_cmd(["ssh-to-age"], stdin_data=ssh_pub_input, capture=True)
                    if ssh_derived_pub == current_pub:
                        log.warn("setup", "age key was derived from SSH and doesn't match .sops.yaml")
                        log.hint("This usually means your SSH key was rotated on a new device.")
                except subprocess.CalledProcessError:
                    pass

            log.warn("setup", "age key exists but is not in .sops.yaml for this device")
            log.hint("It will be added during save.")
            return current_pub

    # No age key file — try SSH derivation or generate new
    ssh_key = HOME_DIR / ".ssh" / "id_ed25519"
    if ssh_key.exists():
        try:
            result = run_cmd(["ssh-to-age", "-private-key", "-i", str(ssh_key)], capture=True)
            store_device_age_key(result)
            with open(str(ssh_key) + ".pub") as f:
                pub_input = f.read()
            public_key = run_cmd(["ssh-to-age"], stdin_data=pub_input, capture=True)
            if public_key:
                return public_key
        except subprocess.CalledProcessError:
            pass

    backup_sensitive_file(AGE_KEY_FILE)
    return generate_device_age_key()


# setup/key Git helpers are imported from key.py


# ==========================================
# MENUCONFIG UI — single Application with mode switching
# ==========================================


@dataclass
class SetupResult:
    values: dict
    exclusions: dict[str, list[str]]


class AppState:
    """State for the menuconfig Application."""
    def __init__(
        self,
        values: dict,
        manifest: dict | None = None,
        exclusions: dict[str, list[str]] | None = None,
    ):
        self.values = values
        self.manifest = manifest
        self.policy_groups = groups_for_manifest(manifest)
        self.original_exclusions = normalize_exclusions(exclusions, self.policy_groups)
        self.exclusions = normalize_exclusions(exclusions, self.policy_groups)
        self.mode = "list"  # list | policy | policy_search | edit_text | edit_choice
        self.cursor = 0
        self.editing_field: Optional[FieldDef] = None
        self.edit_buffer = Buffer()
        self.search_buffer = Buffer()
        self.choice_cursor = 0
        self.policy_group = 0
        self.policy_cursor = 0
        self.policy_query = ""
        self.policy_notice = ""
        self.error_msg = ""
        self.result: Optional[SetupResult] = None  # set on save/quit


def init_values() -> dict:
    existing_config = read_machine_nix()
    evaluated_config = manifest_settings(machine_manifest())
    existing_secrets, _ = read_secrets_yaml()
    values = {}
    for f in ALL_FIELDS:
        if f.condition and not f.condition(values):
            continue
        if f.dest == "machine" and f.path in evaluated_config:
            values[f.path] = evaluated_config[f.path]
        elif f.dest == "machine" and f.path in existing_config:
            values[f.path] = existing_config[f.path]
        elif f.dest == "secret" and f.path in existing_secrets:
            values[f.path] = existing_secrets[f.path]
        else:
            values[f.path] = f.default_fn()
    return values


def handle_decrypt_failure() -> tuple[dict, bool]:
    """Layered guidance when sops decryption fails on a new device.
    Delegates to key_import() interactive mode from key.py."""
    log.console.print(Panel(
        "[bold red]sops decryption failed[/bold red]\n\n"
        "This usually means you're on a [bold]new device[/bold] and the age key\n"
        "doesn't match the one that encrypted secrets.yaml.\n\n"
        "To restore your existing secrets, you need a key from another device.",
        title="New Device Detected", border_style="red"))

    pub = key_import(register=False)  # setup registers/re-encrypts only after save confirmation
    if pub:
        secrets, ok = read_secrets_yaml()
        if ok:
            log.ok("setup", "decryption successful — existing secrets restored")
            return secrets, False
        else:
            log.warn("setup", "key imported but decryption still failed, try another key")

    log.warn("setup", "proceeding with empty secrets — re-enter all values in the menuconfig UI")
    return {}, True


def get_visible_fields(values: dict) -> list:
    items = []
    for f in ALL_FIELDS:
        if f.ignore:
            continue
        if f.condition and not f.condition(values):
            continue
        items.append(f)
    return items


def _display_value(field: FieldDef, value: object) -> str:
    if field.dest == "secret":
        return "<set>" if value else "<empty>"
    return str(value)


def _list_text(state: AppState) -> list:
    items = get_visible_fields(state.values)
    if state.cursor >= len(items):
        state.cursor = max(0, len(items) - 1)

    lines = []
    current_group = None
    for i, f in enumerate(items):
        if f.group != current_group:
            current_group = f.group
            lines.append(("class:group", f"\n  ── {current_group} ──\n"))
        val = _display_value(f, state.values.get(f.path, ""))
        tag = "[S] " if f.dest == "secret" else ""
        if f.choices:
            tag += f"({','.join(f.choices)}) "
        if i == state.cursor:
            lines.append(("class:cursor", f"  ► {tag}{f.prompt}: {val}\n"))
        else:
            lines.append(("class:normal", f"    {tag}{f.prompt}: {val}\n"))
    return lines


def _policy_items(state: AppState):
    group = state.policy_groups[state.policy_group]
    items = build_software_items(
        state.manifest,
        state.exclusions,
        state.original_exclusions,
        state.policy_query,
        groups=state.policy_groups,
    )[group.key]
    if state.policy_cursor >= len(items):
        state.policy_cursor = max(0, len(items) - 1)
    return items


def _toggle_policy_item(state: AppState) -> None:
    items = _policy_items(state)
    if not items:
        return
    item = items[state.policy_cursor]
    group = state.policy_groups[state.policy_group]
    if item.locked:
        state.policy_notice = f"{item.name} is excluded outside the managed machine block."
    elif item.checked:
        set_excluded(
            state.exclusions,
            group.key,
            item.id,
            True,
            groups=state.policy_groups,
        )
        state.policy_notice = f"{item.name} will be disabled on this machine."
    else:
        set_excluded(
            state.exclusions,
            group.key,
            item.id,
            False,
            groups=state.policy_groups,
        )
        state.policy_notice = f"{item.name} will be enabled on this machine."


def _policy_text(state: AppState) -> list:
    if not state.manifest:
        return [
            ("class:error", "\n  Nix evaluation is unavailable.\n"),
            ("class:hint", "  Fix the machine evaluation, then reopen setup.\n"),
        ]

    group = state.policy_groups[state.policy_group]
    items = _policy_items(state)
    disabled = sum(not item.checked for item in items)
    machine_id = state.manifest.get("id", "current")
    query = state.policy_query or "<none>"
    notice = state.policy_notice or "Space toggles this machine's exclusion list."
    lines = [
        ("class:group", f"  {group.label}  ({state.policy_group + 1}/{len(state.policy_groups)})\n"),
        ("class:dim", f"  Machine: {machine_id}  Items: {len(items)}  Disabled: {disabled}\n"),
        ("class:hint", f"  Search: {query}\n"),
        (("class:error" if state.policy_notice else "class:hint"), f"  {notice}\n"),
    ]
    if not items:
        lines.append(("class:hint", "\n  No matching software items.\n"))
        return lines

    for index, item in enumerate(items):
        marker = "[x]" if item.checked else ("[-]" if item.locked else "[ ]")
        annotations = []
        if item.source_id:
            annotations.append(item.source_id)
        if item.changed:
            annotations.append("pending")
        if item.locked:
            annotations.append("external exclusion")
        elif item.stale:
            annotations.append("stale exclusion")
        elif item.managed:
            annotations.append("machine exclusion")
        suffix = f"  ({', '.join(annotations)})" if annotations else ""
        style = "class:cursor" if index == state.policy_cursor else (
            "class:dim" if item.locked else "class:normal"
        )
        lines.append((style, f"  {marker} {item.name}{suffix}\n"))
    return lines


def _edit_frame_title(state: AppState) -> list:
    """Frame title for edit_text mode — includes error if present."""
    f = state.editing_field
    if state.error_msg:
        return [("class:error", f"Edit: {f.prompt}  ✗ {state.error_msg}")]
    return [("class:title", f"Edit: {f.prompt}")]


def _edit_context(state: AppState) -> list:
    """Dim current value line shown inside the edit frame."""
    f = state.editing_field
    val = _display_value(f, state.values.get(f.path, ""))
    return [("class:dim", f"  Current: {val}")]


def _choice_frame_title(state: AppState) -> list:
    f = state.editing_field
    return [("class:title", f"Select: {f.prompt}")]

def _choice_text(state: AppState) -> list:
    f = state.editing_field
    choices = f.choices
    lines = []
    for i, c in enumerate(choices):
        if i == state.choice_cursor:
            lines.append(("class:cursor", f"  ► {c}\n"))
        else:
            lines.append(("class:normal", f"    {c}\n"))
    return lines


def build_application(state: AppState) -> Application:
    """Build a single Application with mode-switching layout."""
    is_list = Condition(lambda: state.mode == "list")
    is_policy = Condition(lambda: state.mode == "policy")
    is_policy_search = Condition(lambda: state.mode == "policy_search")
    is_edit_text = Condition(lambda: state.mode == "edit_text")
    is_edit_choice = Condition(lambda: state.mode == "edit_choice")

    # --- Title bar (always visible) ---
    title_content = FormattedTextControl(lambda: [("class:title", (
        "  envY Setup — evaluated software policy"
        if state.mode in {"policy", "policy_search"}
        else "  envY Setup — menuconfig"
    ))])
    title_window = Window(content=title_content, height=1)

    # --- List mode ---
    list_content = FormattedTextControl(lambda: _list_text(state), focusable=True)
    list_window = Window(content=list_content)
    list_container = ConditionalContainer(content=list_window, filter=is_list)

    policy_content = FormattedTextControl(
        lambda: _policy_text(state),
        focusable=True,
        get_cursor_position=lambda: Point(x=0, y=4 + state.policy_cursor),
    )
    policy_window = Window(content=policy_content)
    policy_container = ConditionalContainer(content=policy_window, filter=is_policy)

    search_context = FormattedTextControl(lambda: [
        ("class:dim", f"  Current filter: {state.policy_query or '<none>'}")
    ])
    search_context_window = Window(content=search_context, height=1)
    search_buffer_control = BufferControl(buffer=state.search_buffer)
    search_buffer_window = Window(content=search_buffer_control, height=1)
    policy_search_container = ConditionalContainer(
        content=Frame(
            title="Filter software names",
            body=HSplit([search_context_window, search_buffer_window]),
        ),
        filter=is_policy_search,
    )

    # --- Edit text mode (framed) ---
    edit_context = FormattedTextControl(lambda: _edit_context(state))
    edit_context_window = Window(content=edit_context, height=1)
    edit_buffer_control = BufferControl(
        buffer=state.edit_buffer,
        input_processors=[
            ConditionalProcessor(
                PasswordProcessor(),
                filter=Condition(
                    lambda: state.editing_field is not None
                    and state.editing_field.dest == "secret"
                ),
            )
        ],
    )
    edit_buffer_window = Window(content=edit_buffer_control, height=1)
    edit_text_container = ConditionalContainer(
        content=Frame(
            title=lambda: _edit_frame_title(state),
            body=HSplit([edit_context_window, edit_buffer_window]),
        ),
        filter=is_edit_text,
    )

    # --- Edit choice mode (framed) ---
    choice_content = FormattedTextControl(lambda: _choice_text(state))
    choice_window = Window(content=choice_content)
    choice_container = ConditionalContainer(
        content=Frame(
            title=lambda: _choice_frame_title(state),
            body=choice_window,
        ),
        filter=is_edit_choice,
    )

    # --- Bottom bar (always visible, context-sensitive) ---
    def bottom_text():
        if state.mode == "list":
            return [("class:bottom", "  Enter: edit  │  p: software policy  │  s: save & exit  │  q/Esc: quit  │  ↑↓: navigate")]
        elif state.mode == "policy":
            return [("class:bottom", "  ←→: group  │  ↑↓: move  │  Space: toggle  │  /: search  │  r: reset  │  p/Esc: back")]
        elif state.mode == "policy_search":
            return [("class:bottom", "  Enter: apply filter  │  Esc: cancel")]
        elif state.mode == "edit_text":
            return [("class:bottom", "  Enter: confirm  │  Esc: cancel")]
        elif state.mode == "edit_choice":
            return [("class:bottom", "  Enter: select  │  Esc: cancel  │  ↑↓: navigate")]
        return []

    bottom_content = FormattedTextControl(bottom_text)
    bottom_window = Window(content=bottom_content, height=1)

    layout = Layout(HSplit([
        title_window,
        list_container,
        policy_container,
        policy_search_container,
        edit_text_container,
        choice_container,
        bottom_window,
    ]))

    # --- Key bindings ---
    kb = KeyBindings()

    # Navigation — only in list and choice modes
    @kb.add("up", filter=is_list | is_policy | is_edit_choice)
    def _(event):
        if state.mode == "list":
            items = get_visible_fields(state.values)
            state.cursor = max(0, state.cursor - 1)
        elif state.mode == "policy":
            state.policy_cursor = max(0, state.policy_cursor - 1)
        elif state.mode == "edit_choice":
            choices = state.editing_field.choices
            state.choice_cursor = max(0, state.choice_cursor - 1)
        event.app.invalidate()

    @kb.add("down", filter=is_list | is_policy | is_edit_choice)
    def _(event):
        if state.mode == "list":
            items = get_visible_fields(state.values)
            state.cursor = min(len(items) - 1, state.cursor + 1)
        elif state.mode == "policy":
            row_count = len(_policy_items(state))
            state.policy_cursor = min(max(0, row_count - 1), state.policy_cursor + 1)
        elif state.mode == "edit_choice":
            choices = state.editing_field.choices
            state.choice_cursor = min(len(choices) - 1, state.choice_cursor + 1)
        event.app.invalidate()

    # Enter — mode-specific behavior
    @kb.add("enter", filter=is_list)
    def _(event):
        items = get_visible_fields(state.values)
        if not items:
            return
        f = items[state.cursor]
        state.editing_field = f
        state.error_msg = ""
        if f.choices:
            current_val = state.values.get(f.path, f.default_fn())
            state.choice_cursor = f.choices.index(current_val) if current_val in f.choices else 0
            state.mode = "edit_choice"
        else:
            current_val = state.values.get(f.path, f.default_fn())
            state.edit_buffer.text = current_val
            state.edit_buffer.cursor_position = len(current_val)
            state.mode = "edit_text"
            event.app.layout.focus(edit_buffer_window)
        event.app.invalidate()

    @kb.add("enter", filter=is_edit_text)
    def _(event):
        new_val = state.edit_buffer.text
        # Validate using field validators
        for v in state.editing_field.validators:
            error = v(new_val)
            if error:
                state.error_msg = error
                event.app.invalidate()
                return
        state.values[state.editing_field.path] = new_val
        state.error_msg = ""
        state.mode = "list"
        event.app.invalidate()

    @kb.add("enter", filter=is_edit_choice)
    def _(event):
        selected = state.editing_field.choices[state.choice_cursor]
        state.values[state.editing_field.path] = selected
        state.mode = "list"
        event.app.invalidate()

    @kb.add("left", filter=is_policy)
    def _(event):
        state.policy_group = (state.policy_group - 1) % len(state.policy_groups)
        state.policy_cursor = 0
        state.policy_notice = ""
        event.app.invalidate()

    @kb.add("right", filter=is_policy)
    def _(event):
        state.policy_group = (state.policy_group + 1) % len(state.policy_groups)
        state.policy_cursor = 0
        state.policy_notice = ""
        event.app.invalidate()

    @kb.add(" ", filter=is_policy)
    def _(event):
        _toggle_policy_item(state)
        event.app.invalidate()

    @kb.add("r", filter=is_policy)
    def _(event):
        state.exclusions = normalize_exclusions(
            state.original_exclusions,
            state.policy_groups,
        )
        state.policy_notice = "Pending software changes reset."
        event.app.invalidate()

    @kb.add("/", filter=is_policy)
    def _(event):
        state.search_buffer.text = state.policy_query
        state.search_buffer.cursor_position = len(state.search_buffer.text)
        state.mode = "policy_search"
        event.app.layout.focus(search_buffer_window)
        event.app.invalidate()

    @kb.add("enter", filter=is_policy_search)
    def _(event):
        state.policy_query = state.search_buffer.text.strip()
        state.policy_cursor = 0
        state.policy_notice = ""
        state.mode = "policy"
        event.app.layout.focus(policy_window)
        event.app.invalidate()

    @kb.add("escape", filter=is_policy_search)
    def _(event):
        state.mode = "policy"
        event.app.layout.focus(policy_window)
        event.app.invalidate()

    @kb.add("p", filter=is_list)
    def _(event):
        state.mode = "policy"
        state.policy_notice = ""
        event.app.layout.focus(policy_window)
        event.app.invalidate()

    @kb.add("p", filter=is_policy)
    @kb.add("escape", filter=is_policy)
    def _(event):
        state.mode = "list"
        event.app.layout.focus(list_window)
        event.app.invalidate()

    # Escape — cancel edit or quit from list
    @kb.add("escape", filter=is_edit_text | is_edit_choice)
    def _(event):
        state.error_msg = ""
        state.mode = "list"
        event.app.invalidate()

    @kb.add("escape", filter=is_list)
    @kb.add("q", filter=is_list)
    @kb.add("q", filter=is_policy)
    def _(event):
        state.result = None
        event.app.exit()

    # Save — only from list mode
    @kb.add("s", filter=is_list)
    def _(event):
        state.result = SetupResult(
            values=dict(state.values),
            exclusions=normalize_exclusions(state.exclusions, state.policy_groups),
        )
        event.app.exit()

    style = PtStyle.from_dict({
        "title": "#ansicyan bold",
        "group": "#ansicyan bold",
        "cursor": "bg:#ansicyan #ansiblack bold",
        "normal": "",
        "dim": "#ansigray",
        "hint": "#ansigray",
        "error": "#ansired bold",
        "bottom": "#ansigray",
        "frame.border": "#ansigray",
        "frame.title": "#ansicyan bold",
    })

    return Application(layout=layout, key_bindings=kb, style=style, full_screen=True)


def menuconfig_loop(
    initial_values: dict,
    manifest: dict | None = None,
    exclusions: dict[str, list[str]] | None = None,
) -> Optional[SetupResult]:
    state = AppState(values=dict(initial_values), manifest=manifest, exclusions=exclusions)
    app = build_application(state)
    app.run()
    return state.result


def show_changes(
    old_values: dict,
    new_values: dict,
    old_exclusions: dict[str, list[str]],
    new_exclusions: dict[str, list[str]],
    groups,
) -> bool:
    """Display changed fields without exposing secret values."""
    changes = []
    for f in ALL_FIELDS:
        if f.ignore:
            continue
        old = old_values.get(f.path, "")
        new = new_values.get(f.path, "")
        if old != new:
            tag = "SECRET" if f.dest == "secret" else "MACHINE"
            if f.dest == "secret":
                old = _display_value(f, old)
                new = _display_value(f, new)
            changes.append((f"{tag}: {f.prompt}", old, new))

    for label, disabled, enabled in software_changes(old_exclusions, new_exclusions, groups):
        if disabled:
            changes.append((f"SOFTWARE: {label}", "enabled", f"disable: {', '.join(disabled)}"))
        if enabled:
            changes.append((f"SOFTWARE: {label}", "excluded", f"enable: {', '.join(enabled)}"))

    if not changes:
        log.hint("No changes detected")
        return False

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Field", style="cyan")
    table.add_column("Old", style="red")
    table.add_column("New", style="green")
    for row in changes:
        table.add_row(*row)

    log.console.print(Panel(table, title="Changes Summary", border_style="yellow"))
    return True


def prompt_yes_no(question: str) -> bool:
    """Require an explicit yes or no; blank and invalid answers retry."""
    while True:
        try:
            answer = pt_prompt(f"? {question} [y/n]: ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        log.warn("input", "please answer y/yes or n/no")


def confirm_save() -> bool:
    """Require an explicit confirmation before writing setup changes."""
    return prompt_yes_no("Apply changes and save?")


def validate_required_fields(values: dict) -> bool:
    """Reject empty/invalid required fields before the destructive save.

    Reuses envy.config._validate_field so the checks and hints match the
    `envy config refine` CLI path. Returns True when every applicable field
    passes; logs per-field errors and returns False otherwise.
    """
    report = RefineReport()
    for f in ALL_FIELDS:
        if f.condition and not f.condition(values):
            continue
        if f.path not in values:
            continue
        _validate_field(f, values[f.path], report, scope="setup")
    return not report.errors


def save_all(
    values: dict,
    exclusions: dict[str, list[str]],
    original_machine_source: str,
    *,
    replace_secrets: bool = False,
    groups=None,
) -> None:
    target = machine_file(current_machine_id())
    if source_digest(target.read_text()) != source_digest(original_machine_source):
        raise ConcurrentMachineEdit(
            f"machine configuration changed while setup was open: {target}"
        )

    existing_secrets, _ = read_secrets_yaml()
    transaction_paths = [
        target,
        SOPS_YAML,
        SECRETS_FILE,
        RECOVERY_KEY_FILE,
        AGE_KEY_FILE,
        DEVICE_LABEL_FILE,
    ]

    log.step("setup", "preparing transactional machine, key, and secret update")
    with FileTransaction(transaction_paths) as transaction:
        write_machine_nix(values)
        write_and_validate_exclusions(exclusions, target, groups=groups)
        log.ok("machine", "machine configuration evaluated successfully", path=str(target))

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=log.console,
        ) as progress:

            t1 = progress.add_task("Setting up age key...", total=None)
            public_key = setup_age_key()
            progress.update(t1, completed=True, description=f"Age key: [green]{public_key[:20]}...[/green]")

            t2 = progress.add_task("Updating .sops.yaml...", total=None)
            keys = read_sops_yaml_keys()
            label = ensure_sops_label()
            sops_updated = label not in keys or keys[label] != public_key
            if sops_updated:
                keys[label] = public_key
                write_sops_yaml_keys(keys)

            if "recovery" not in read_sops_yaml_keys():
                from envy.key import key_add_recovery
                key_add_recovery(reencrypt_secrets=False)
                sops_updated = True

            progress.update(
                t2,
                completed=True,
                description=(
                    "[green].sops.yaml updated[/green]"
                    if sops_updated else "[dim].sops.yaml up-to-date (skipped)[/dim]"
                ),
            )

            secrets_changed = (
                not SECRETS_FILE.exists()
                or replace_secrets
                or any(
                    str(values.get(f.path, "")) != str(existing_secrets.get(f.path, ""))
                    for f in SECRET_FIELDS
                )
            )

            if secrets_changed:
                t4 = progress.add_task("Encrypting secrets.yaml...", total=None)
                write_secrets_yaml(values, replace=replace_secrets)
                progress.update(t4, completed=True, description="[green]secrets.yaml encrypted[/green]")
            elif sops_updated:
                t5 = progress.add_task("Re-encrypting secrets with updated keys...", total=None)
                run_sops_updatekeys()
                progress.update(t5, completed=True)
            else:
                progress.add_task("[dim]secrets.yaml unchanged (skipped)[/dim]")

        transaction.commit()

    log.step("setup", "checking managed files for a Git commit")
    git_commit_setup_files(target)


# ==========================================
# MAIN
# ==========================================


def ensure_machine_configuration(machine_id: str) -> bool:
    """Offer only the import/copy choice when this machine file is missing."""
    selected = validate_machine_id(machine_id)
    target = machine_file(selected)
    if target.exists():
        set_device_machine_id(selected)
        return True

    log.warn("host", "machine configuration is missing", path=str(target))
    try:
        if not prompt_yes_no("Create this machine configuration now?"):
            log.hint(f"Create it later with: envy host init {selected}")
            return False

        while True:
            mode = pt_prompt("? Creation mode (import/copy) [import]: ").strip().lower() or "import"
            if mode in {"import", "copy"}:
                break
            log.error("host", "creation mode must be import or copy")
        initialize_machine(selected, mode)
        return True
    except (EOFError, KeyboardInterrupt):
        log.hint(f"Create it later with: envy host init {selected}")
        return False

def main() -> int:
    # Persist the existing device identity even when the user opens setup and
    # exits without changing any config fields. Newly generated/imported keys
    # are handled again in save_all after key setup completes.
    if AGE_KEY_FILE.exists():
        ensure_sops_label()

    machine_id = current_machine_id()
    if not ensure_machine_configuration(machine_id):
        return 2

    selected_machine_file = machine_file(machine_id)
    original_machine_source = selected_machine_file.read_text()
    manifest = machine_manifest()
    policy_groups = groups_for_manifest(manifest, include_empty=True)
    try:
        original_exclusions = read_managed_exclusions(selected_machine_file, policy_groups)
    except SoftwarePolicyError as exc:
        log.error("software", str(exc))
        log.hint("Fix the managed exclusions block, then reopen envy setup.")
        return 1

    existing_config = read_machine_nix()
    evaluated_config = manifest_settings(manifest)
    existing_secrets, decrypt_ok = read_secrets_yaml()
    replace_secrets = False

    # Layered guidance for new device: if decryption failed, try key import before menuconfig
    if not decrypt_ok and SECRETS_FILE.exists():
        restored_secrets, replace_secrets = handle_decrypt_failure()
        if restored_secrets:
            existing_secrets = restored_secrets

    # Build initial values from config + (possibly restored) secrets
    values = {}
    for f in ALL_FIELDS:
        if f.condition and not f.condition(values):
            continue
        if f.dest == "machine" and f.path in evaluated_config:
            values[f.path] = evaluated_config[f.path]
        elif f.dest == "machine" and f.path in existing_config:
            values[f.path] = existing_config[f.path]
        elif f.dest == "secret" and f.path in existing_secrets:
            values[f.path] = existing_secrets[f.path]
        else:
            values[f.path] = f.default_fn()
    old_values = dict(values)

    result = menuconfig_loop(old_values, manifest, original_exclusions)

    if result is None:
        log.hint("Quit without saving.")
        return 2

    new_values = result.values
    new_exclusions = result.exclusions

    log.console.print()
    has_changes = show_changes(
        old_values,
        new_values,
        original_exclusions,
        new_exclusions,
        policy_groups,
    )

    if not has_changes:
        missing_machine_fields = [
            field.path for field in MACHINE_FIELDS if field.path not in existing_config
        ]
        if missing_machine_fields:
            log.warn(
                "machine",
                "initial managed machine block must be written",
                fields=len(missing_machine_fields),
            )
            has_changes = True
        else:
            log.hint("No changes to apply.")
            return 0

    # Catch empty/invalid required fields before the destructive save. Otherwise
    # write_machine_nix persists e.g. `envy.llm.steps.url = ""`, the Nix manifest
    # rejects it, and the FileTransaction rolls everything back — silently
    # discarding the user's edits with only a cryptic `""` from the evaluator.
    if not validate_required_fields(new_values):
        log.hint("Fix the fields above, then reopen envy setup.")
        return 1

    if not confirm_save():
        log.warn("setup", "changes cancelled")
        return 2

    log.console.print()
    try:
        save_all(
            new_values,
            new_exclusions,
            original_machine_source,
            replace_secrets=replace_secrets,
            groups=policy_groups,
        )
    except CommandError as exc:
        render_command_error(exc)
        log.error("setup", "transaction failed; managed files were restored")
        return exc.returncode or 1
    except (OSError, RuntimeError, ValueError, SoftwarePolicyError, ConcurrentMachineEdit) as exc:
        log.error("setup", str(exc))
        log.error("setup", "transaction failed; managed files were restored")
        return 1
    log.console.print()
    log.console.print(Panel("[bold green]All files saved successfully![/bold green]", border_style="green"))

    # Ask if user wants to apply immediately
    if prompt_yes_no("Apply configuration now (envy apply)?"):
        log.step("setup", "running envy apply")
        from envy.main import cmd_apply
        try:
            cmd_apply()
        except CommandError as exc:
            render_command_error(exc)
            log.warn("setup", "files were saved, but apply failed")
            return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
