"""Software CLI commands for the selected machine."""

import json
import sys
from dataclasses import replace
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from envy import log
from envy.evaluation import machine_manifest, manifest_software_groups
from envy.jsonio import emit, emit_error
from envy.mutation import offer_mutation_commit
from envy.search.index import RegistryIndex
from envy.software.audit import audit_policy, explain_policy_item
from envy.software.completion import (
    complete_add_groups,
    complete_add_items,
    complete_add_targets,
    complete_disable_groups,
    complete_disable_items,
    complete_enable_groups,
    complete_enable_items,
    complete_remove_groups,
    complete_remove_items,
    complete_remove_targets,
    complete_search_sources,
    complete_why_groups,
    complete_why_items,
)
from envy.software.model import (
    SelectionGroup,
    SoftwarePlan,
    SoftwarePolicyError,
    _evaluated_item_state,
    build_software_items,
    groups_for_manifest,
    require_group,
    set_excluded,
)
from envy.software.planner import _resolve_existing_item, _resolve_new_include, build_desired_plan
from envy.software.policy import (
    machine_file,
    read_managed_exclusions,
    source_digest,
    write_and_validate_exclusions,
    write_and_validate_software_policy,
)
from envy.software.policy_text import _parse_managed_policy_source
from envy.sources.completion import complete_add_source_ids, complete_search_source_ids
from envy.utils import current_machine_id


def _group_matches_item(
    manifest: dict[str, Any], groups: tuple[SelectionGroup, ...], item_value: str,
) -> list[SelectionGroup]:
    source = machine_file().read_text()
    _, exclusions = _parse_managed_policy_source(source, groups)
    items_by_group = build_software_items(manifest, exclusions, groups=groups)
    needle = item_value.casefold()
    matches: list[SelectionGroup] = []
    for group in groups:
        if any(
            needle in {
                item.id.casefold(),
                item.name.casefold(),
                (item.ref or "").casefold(),
            }
            for item in items_by_group[group.key]
        ):
            matches.append(group)
            continue
        if group.ecosystem in {"homebrew", "npm", "pypi", "native"}:
            indexed = RegistryIndex().lookup(group.ecosystem, group.kind, item_value)
            if indexed is not None:
                matches.append(group)
    return matches


def _reference_groups(
    groups: tuple[SelectionGroup, ...], item_value: str,
) -> list[SelectionGroup]:
    if ":" not in item_value:
        return []
    ecosystem, reference = item_value.split(":", 1)
    if ecosystem == "flake":
        ecosystem = "nix"
    matches = [group for group in groups if group.ecosystem == ecosystem]
    if ecosystem == "homebrew" and "/" in reference:
        kind = reference.split("/", 1)[0]
        aliases = {"cask": "cask", "formula": "formula", "tap": "repository"}
        if kind in aliases:
            matches = [group for group in matches if group.kind == aliases[kind]]
    return matches


def _choose_cli_group(
    item_value: str,
    *,
    action: str,
    json_output: bool,
) -> str:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    matches = _group_matches_item(manifest, groups, item_value)
    if not matches:
        matches = _reference_groups(groups, item_value)
    if not matches and action == "add":
        matches = [group for group in groups if group.editable_include]
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        if not json_output:
            log.info(
                "software", "selected compatible group",
                group=matches[0].key, label=matches[0].label,
            )
        return matches[0].key
    if json_output or not sys.stdin.isatty():
        if not matches:
            raise typer.BadParameter(
                f"cannot infer a software group for {item_value}; pass --group <group>"
            )
        choices = ", ".join(group.key for group in matches)
        raise typer.BadParameter(
            f"software group is ambiguous for {item_value}; pass --group with one of: {choices}"
        )
    if not matches:
        raise typer.BadParameter(f"no compatible software group found for: {item_value}")

    table = Table(title=f"Choose how envY should manage {item_value}")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Type")
    table.add_column("Canonical group", style="dim")
    for index, group in enumerate(matches, start=1):
        table.add_row(str(index), group.label, group.key)
    log.console.print(table)
    while True:
        selected = typer.prompt("Software type", default=1, type=int)
        if 1 <= selected <= len(matches):
            return matches[selected - 1].key
        log.warn("software", f"choose a number between 1 and {len(matches)}")


def _render_plan(plan: SoftwarePlan) -> None:
    table = Table(title=f"Software plan - {plan.action} {plan.group.key}/{plan.item_id}")
    table.add_column("Selection")
    table.add_column("Change")
    table.add_column("Stable ID")
    table.add_column("Ownership")
    rows = [
        *(('include', '+', item, 'envy managed') for item in plan.include_added),
        *(('include', '-', item, 'envy managed') for item in plan.include_removed),
        *(('exclude', '+', item, 'envy managed') for item in plan.exclude_added),
        *(('exclude', '-', item, 'envy managed') for item in plan.exclude_removed),
    ]
    if rows:
        for row in rows:
            table.add_row(*row)
    else:
        table.add_row("-", "=", plan.item_id, "no managed change")
    log.console.print(table)
    log.info(
        "software",
        "expected policy",
        included=str(plan.expected_included).lower(),
        excluded=str(plan.expected_excluded).lower(),
        effective=str(plan.expected_effective).lower(),
        clean=str(plan.clean).lower(),
    )
    if plan.blocked:
        log.error("software", "plan is blocked", reason=plan.blocked)


def _emit_mutation_result(
    plan: SoftwarePlan,
    *,
    result: str,
    machine: str | None = None,
    message: str | None = None,
    command: str | None = None,
) -> None:
    data: dict[str, Any] = {
        "result": result,
        "plan": plan.to_dict(),
    }
    if machine is not None:
        data["machine"] = machine
    if message is not None:
        data["message"] = message
    emit(command or f"software.{plan.action}", data=data)


def _apply_desired_state(
    group_value: str,
    item_value: str,
    *,
    action: str,
    clean: bool,
    yes: bool,
    dry_run: bool,
    explicit_ref: str | None,
    offline: bool = False,
    refresh: bool = False,
    json_output: bool = False,
    source_id: str | None = None,
    command_prefix: str = "software",
) -> None:
    command = f"{command_prefix}.{action}"
    try:
        manifest = _manifest_or_exit()
        groups = groups_for_manifest(manifest, include_empty=True)
        group = require_group(group_value, groups)
        target = machine_file()
        original_source = target.read_text()
        digest = source_digest(original_source)
        includes, exclusions = _parse_managed_policy_source(original_source, groups)
    except (OSError, SoftwarePolicyError, typer.BadParameter) as exc:
        if json_output:
            emit_error(command, str(exc), code="invalid-policy")
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    try:
        resolved = _resolve_new_include(
            action=action,
            group=group,
            item_value=item_value,
            explicit_ref=explicit_ref,
            manifest=manifest,
            includes=includes,
            exclusions=exclusions,
            offline=offline,
            refresh=refresh,
            quiet=json_output,
            source_id=source_id,
        )
        if source_id and group.ecosystem == "nix":
            wanted = "nix:" + ((explicit_ref or item_value)[6:].split("#", 1)[0]) if (explicit_ref or item_value).startswith("flake:") else "nix:nixpkgs"
            if wanted != source_id:
                raise SoftwarePolicyError("Nix reference conflicts with --source-id")
        plan = build_desired_plan(
            action=action,
            group=group,
            item_value=item_value,
            manifest=manifest,
            includes=includes,
            exclusions=exclusions,
            clean=clean,
            explicit_ref=explicit_ref,
            resolved=resolved,
        )
        if source_id:
            plan = replace(plan, source_id=source_id)
    except (SoftwarePolicyError, ValueError) as exc:
        if json_output:
            emit_error(command, str(exc), code=getattr(exc, "code", "invalid-policy"))
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    if not json_output:
        _render_plan(plan)
    if plan.blocked:
        if json_output:
            emit(command, ok=False, data={"result": "blocked", "plan": plan.to_dict()},
                 error={"code": "blocked", "message": plan.blocked})
        raise typer.Exit(code=1)
    if dry_run:
        if json_output:
            _emit_mutation_result(plan, result="dry-run", command=command)
        else:
            log.info("software", "dry run; no files changed")
        return
    if not plan.changed:
        if json_output:
            _emit_mutation_result(plan, result="already-satisfied", command=command)
        else:
            log.ok("software", f"already satisfies {action} intent", group=group.key, item=plan.item_id)
        return
    if json_output and not yes:
        emit(command, ok=False, data={"result": "confirmation-required", "plan": plan.to_dict()},
             error={"code": "confirmation-required", "message": "pass --yes to apply JSON mutation"})
        raise typer.Exit(code=2)
    if not yes and not typer.confirm("Apply this software plan?", default=None):
        if json_output:
            _emit_mutation_result(plan, result="cancelled")
        else:
            log.info("software", "plan cancelled; no files changed")
        return
    updated_exclusions = {key: list(values) for key, values in exclusions.items()}
    updated_exclusions[group.key] = list(plan.exclusions_after)
    try:
        evaluated = write_and_validate_software_policy(
            list(plan.includes_after),
            updated_exclusions,
            group_key=group.key,
            item_id=plan.item_id,
            expected_effective=plan.expected_effective,
            expected_digest=digest,
            groups=groups,
        )
    except SoftwarePolicyError as exc:
        if json_output:
            emit_error(command, str(exc), code="apply-failed")
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    machine = evaluated.get("id", current_machine_id())
    if json_output:
        _emit_mutation_result(plan, result="applied", machine=machine, command=command)
    else:
        log.ok(
            "software",
            "intent applied",
            action=action,
            group=group.key,
            item=plan.item_id,
            effective=str(plan.expected_effective).lower(),
            machine=machine,
        )
    offer_mutation_commit(
        [target],
        f"chore(host): {action} {plan.item_id} on {evaluated.get('id') or current_machine_id()}",
        quiet=json_output,
    )


app = typer.Typer(
    name="software",
    help="Inspect, search, and manage software for the selected machine",
    invoke_without_command=True,
    no_args_is_help=False,
)

cache_app = typer.Typer(
    name="cache",
    help="Inspect or clear the exact registry identity index",
    no_args_is_help=True,
)


@app.callback()
def cmd_software(ctx: typer.Context):
    """List evaluated software policy when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        _print_policy(details=False, json_output=False)


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list(
    details: bool = typer.Option(False, "--details", "-d", help="Include empty groups and package metadata"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List checkbox state for all software groups."""
    _print_policy(details=details, json_output=json_output)


@app.command(name="status")
@app.command(name="st", rich_help_panel="Aliases")
def cmd_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Summarize the selected machine's evaluated software policy."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    managed = read_managed_exclusions(groups=groups)
    items_by_group = build_software_items(manifest, managed, groups=groups)
    included = sum(item.included for items in items_by_group.values() for item in items)
    effective = sum(item.checked for items in items_by_group.values() for item in items)
    excluded = sum(not item.checked for items in items_by_group.values() for item in items)
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", "current"),
        "groups": len(groups),
        "included": included,
        "effective": effective,
        "excluded": excluded,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        log.info(
            "software", "policy status", machine=payload["machine"],
            groups=len(groups), included=included, effective=effective, excluded=excluded,
        )


@app.command(name="audit")
def cmd_audit(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are found"),
):
    """Find stale, redundant, or ambiguous machine software policy."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    source = machine_file().read_text()
    includes, exclusions = _parse_managed_policy_source(source, groups)
    findings = audit_policy(manifest, includes, exclusions, groups)
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", current_machine_id()),
        "findings": [finding.to_dict() for finding in findings],
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    elif findings:
        table = Table(title=f"Software audit - {payload['machine']}")
        table.add_column("Severity")
        table.add_column("Code")
        table.add_column("Group")
        table.add_column("Item")
        table.add_column("Finding")
        for finding in findings:
            style = "yellow" if finding.severity == "warn" else "cyan"
            table.add_row(
                f"[{style}]{finding.severity.upper()}[/{style}]",
                finding.code, finding.group, finding.item_id, finding.message,
            )
        log.console.print(table)
    else:
        log.ok("software", "policy audit found no issues")
    warnings = sum(finding.severity == "warn" for finding in findings)
    if strict and warnings:
        raise typer.Exit(code=1)


@app.command(name="why")
def cmd_why(
    item: str = typer.Argument(
        ..., help="Stable ID, exact name, or canonical reference",
        autocompletion=complete_why_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Restrict to one canonical group",
        autocompletion=complete_why_groups,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Explain why an item is included, excluded, or effective."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    source = machine_file().read_text()
    includes, exclusions = _parse_managed_policy_source(source, groups)
    rows = explain_policy_item(
        manifest, includes, exclusions, groups, item, group_filter=group,
    )
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", current_machine_id()),
        "query": item,
        "matches": rows,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if not rows:
        log.error("software", "no exact policy item match", item=item)
        raise typer.Exit(code=1)
    table = Table(title=f"Why software - {item}")
    for column in (
        "Group", "Item", "Reference", "Included", "Excluded", "Effective", "Ownership",
    ):
        table.add_column(column)
    for row in rows:
        ownership = ", ".join(name for name, enabled in (
            ("machine include", row["machineInclude"]),
            ("shared include", row["externalInclude"]),
            ("machine exclude", row["machineExclude"]),
            ("external exclude", row["externalExclude"]),
        ) if enabled) or "evaluated only"
        table.add_row(
            str(row["group"]), str(row["item"]), str(row["ref"] or ""),
            "yes" if row["included"] else "no",
            "yes" if row["excluded"] else "no",
            "yes" if row["effective"] else "no",
            ownership,
        )
    log.console.print(table)


@app.command(name="disable")
@app.command(name="dis", rich_help_panel="Aliases")
def cmd_disable(
    group: str = typer.Argument(
        ..., help="Canonical software group ID",
        autocompletion=complete_disable_groups,
    ),
    item_id: str = typer.Argument(
        ..., help="Stable item ID to exclude",
        autocompletion=complete_disable_items,
    ),
):
    """Exclude one contributed item on the selected machine."""
    _change_one(group, item_id, excluded=True)


@app.command(name="enable")
@app.command(name="en", rich_help_panel="Aliases")
def cmd_enable(
    group: str = typer.Argument(
        ..., help="Canonical software group ID",
        autocompletion=complete_enable_groups,
    ),
    item_id: str = typer.Argument(
        ..., help="Stable managed exclusion ID to restore",
        autocompletion=complete_enable_items,
    ),
):
    """Remove one machine-local exclusion."""
    _change_one(group, item_id, excluded=False)


@app.command(name="add")
def cmd_add(
    group_or_item: str = typer.Argument(
        ..., help="Software item, or canonical group ID when followed by ITEM",
        autocompletion=complete_add_targets,
    ),
    item: str | None = typer.Argument(
        None, help="Stable ID, package name, or canonical reference in legacy GROUP ITEM form",
        autocompletion=complete_add_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Select a canonical group without an interactive chooser",
        autocompletion=complete_add_groups,
    ),
    source_id: str | None = typer.Option(None, "--source-id", help="Require a concrete software source",
                                       autocompletion=complete_add_source_ids),
    clean: bool = typer.Option(False, "--clean", help="Remove redundant envY-managed state for this item"),
    ref: str | None = typer.Option(None, "--ref", help="Canonical registry reference when item is a custom stable ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed plan without confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display the plan without writing"),
    offline: bool = typer.Option(
        False, "--offline", help="Require a cached identity; stale positive entries are allowed",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore registry identity cache and resolve again",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a stable plan/result document for frontends",
    ),
):
    """Ensure one software item is effective; infer or ask for its software type."""
    if item is not None:
        if group is not None:
            raise typer.BadParameter("use either legacy GROUP ITEM arguments or --group, not both")
        selected_group = group_or_item
        selected_item = item
    else:
        selected_item = group_or_item
        selected_group = group or _choose_cli_group(
            selected_item, action="add", json_output=json_output,
        )
    _apply_desired_state(
        selected_group, selected_item, action="add", clean=clean, yes=yes,
        dry_run=dry_run, explicit_ref=ref, offline=offline, refresh=refresh,
        json_output=json_output, source_id=source_id,
    )


@app.command(name="remove")
@app.command(name="rm", rich_help_panel="Aliases")
def cmd_remove(
    group_or_item: str = typer.Argument(
        ..., help="Software item, or canonical group ID when followed by ITEM",
        autocompletion=complete_remove_targets,
    ),
    item: str | None = typer.Argument(
        None, help="Stable item ID, name, or reference in legacy GROUP ITEM form",
        autocompletion=complete_remove_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Select a canonical group without an interactive chooser",
        autocompletion=complete_remove_groups,
    ),
    clean: bool = typer.Option(False, "--clean", help="Remove redundant envY-managed state for this item"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed plan without confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display the plan without writing"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a stable plan/result document for frontends",
    ),
):
    """Ensure one software item is not effective; infer or ask for its software type."""
    if item is not None:
        if group is not None:
            raise typer.BadParameter("use either legacy GROUP ITEM arguments or --group, not both")
        selected_group = group_or_item
        selected_item = item
    else:
        selected_item = group_or_item
        selected_group = group or _choose_cli_group(
            selected_item, action="rm", json_output=json_output,
        )
    _apply_desired_state(
        selected_group, selected_item, action="rm", clean=clean, yes=yes,
        dry_run=dry_run, explicit_ref=None,
        json_output=json_output,
    )


@app.command(name="search")
@app.command(name="se", rich_help_panel="Aliases")
def cmd_search(
    query: str = typer.Argument(..., help="Package or tool name to search for"),
    source: list[str] | None = typer.Option(
        None, "--source", "-s", help="Restrict search providers",
        autocompletion=complete_search_sources,
    ),
    source_id: list[str] | None = typer.Option(None, "--source-id", help="Filter concrete source identities",
                                             autocompletion=complete_search_source_ids),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100),
    exact: bool = typer.Option(False, "--exact", help="Show exact name matches only"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved registry cache"),
):
    """Search all available software registries."""
    from envy.search import search_and_render

    search_and_render(
        query, sources=source, limit=limit, exact=exact,
        json_output=json_output, refresh=refresh, source_ids=source_id,
    )


@cache_app.command(name="status")
def cmd_cache_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show exact registry index freshness and provider counts."""
    stats = RegistryIndex().stats()
    payload = {"schemaVersion": 1, **stats}
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Software registry index")
    table.add_column("Field")
    table.add_column("Value")
    for key in ("path", "entries", "fresh", "stale", "misses"):
        table.add_row(key, str(stats[key]))
    providers = stats.get("providers")
    if isinstance(providers, dict):
        for provider, count in sorted(providers.items()):
            table.add_row(f"provider.{provider}", str(count))
    log.console.print(table)


@cache_app.command(name="clean")
def cmd_cache_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm exact registry index deletion"),
):
    """Delete the exact registry identity index; future search/add recreates it."""
    index = RegistryIndex()
    if not index.path.exists():
        log.info("software", "registry index is already empty", path=str(index.path))
        return
    if not yes and not typer.confirm(f"Delete registry index {index.path}?", default=None):
        log.info("software", "registry index cleanup cancelled")
        return
    removed = index.clear()
    if removed:
        log.ok("software", "registry index removed", path=str(index.path))
        log.hint("It will be rebuilt by software search and exact add lookups.")


app.add_typer(cache_app, name="cache")


def _manifest_or_exit() -> dict[str, Any]:
    manifest = machine_manifest()
    if manifest is None or not manifest_software_groups(manifest):
        log.error("software", "cannot evaluate the selected machine software manifest")
        raise typer.Exit(code=1)
    return manifest


def _change_one(group_value: str, item_value: str, *, excluded: bool) -> None:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    group = require_group(group_value, groups)
    target = machine_file()
    original_source = target.read_text() if target.exists() else None
    values = read_managed_exclusions(groups=groups)
    if (
        original_source is not None
        and source_digest(target.read_text()) != source_digest(original_source)
    ):
        log.error("software", "machine configuration changed while reading policy")
        raise typer.Exit(code=1)
    items = build_software_items(manifest, values, groups=groups)[group.key]
    try:
        item = _resolve_existing_item(items, item_value)
    except SoftwarePolicyError as exc:
        log.error("software", str(exc), group=group.key, item=item_value)
        raise typer.Exit(code=1)
    if item is None:
        log.error("software", "item is not uniquely identified", group=group.key, item=item_value)
        raise typer.Exit(code=1)
    if excluded:
        if not item.included:
            log.error("software", "item is not contributed by the selected machine", group=group.key, item=item.id)
            raise typer.Exit(code=1)
        if item.locked:
            log.error("software", "item is already excluded outside the managed machine block", group=group.key, item=item.id)
            raise typer.Exit(code=1)
        if item.managed:
            log.ok("software", "already disabled", group=group.key, item=item.id)
            return
    elif item.id not in values[group.key]:
        if item.included and item.checked and not item.locked:
            log.ok("software", "already enabled", group=group.key, item=item.id)
            return
        log.error("software", "item is not excluded by the managed machine block", group=group.key, item=item.id)
        log.hint("External or hand-written exclusions must be edited at their source.")
        raise typer.Exit(code=1)

    set_excluded(values, group.key, item.id, excluded, groups)
    try:
        evaluated = write_and_validate_exclusions(
            values,
            expected_digest=(
                source_digest(original_source) if original_source is not None else None
            ),
            groups=groups,
        )
    except SoftwarePolicyError as exc:
        log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    included_after, effective_after, excluded_after = _evaluated_item_state(
        evaluated, group.key, item.id
    )
    machine = evaluated.get("id", "current")
    if excluded:
        if included_after and excluded_after and not effective_after:
            log.ok("software", "disabled", group=group.key, item=item.id, machine=machine)
            commit_action = f"disable {item.id}"
        else:
            log.warn(
                "software", "managed exclusion saved, but evaluated state is unexpected",
                group=group.key, item=item.id, machine=machine,
            )
            commit_action = f"exclude {item.id}"
    elif not included_after and excluded_after:
        log.warn(
            "software",
            "managed stale exclusion removed; item remains excluded by another policy",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("The item is not contributed, and an external stale exclusion still exists.")
        commit_action = f"remove managed stale exclusion for {item.id}"
    elif not included_after:
        log.ok(
            "software", "stale exclusion removed",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("The item is not contributed by any active module, so no software was enabled.")
        commit_action = f"remove stale exclusion for {item.id}"
    elif effective_after:
        log.ok("software", "enabled", group=group.key, item=item.id, machine=machine)
        commit_action = f"enable {item.id}"
    elif excluded_after:
        log.warn(
            "software", "managed exclusion removed; item remains excluded by another policy",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("Edit the hand-written or imported exclusion at its source to enable the item.")
        commit_action = f"remove managed exclusion for {item.id}"
    else:
        log.warn(
            "software", "managed exclusion removed, but item is still not effective",
            group=group.key, item=item.id, machine=machine,
        )
        commit_action = f"remove managed exclusion for {item.id}"
    target_machine = evaluated.get("id") or current_machine_id()
    offer_mutation_commit(
        [machine_file()],
        f"chore(host): {commit_action} on {target_machine}",
    )


def _print_policy(*, details: bool, json_output: bool = False) -> None:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=details)
    managed = read_managed_exclusions(groups=groups)
    items_by_group = build_software_items(manifest, managed, groups=groups)
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machine": manifest.get("id", current_machine_id()),
            "platform": manifest.get("platform"),
            "groups": [{
                "id": group.key,
                "label": group.label,
                "ecosystem": group.ecosystem,
                "scope": group.scope,
                "kind": group.kind,
                "items": [{
                    "id": item.id,
                    "name": item.name,
                    "version": item.version,
                    "ref": item.ref,
                    "sourceId": item.source_id,
                    "resolvedVersion": item.resolved_version,
                    "versionConstraintApplied": not (item.version and group.ecosystem == "native"),
                    "included": item.included,
                    "effective": item.checked,
                    "machineExclude": item.managed,
                    "externalExclude": item.locked,
                    "stale": item.stale,
                } for item in items_by_group[group.key]],
            } for group in groups],
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f"Machine software - {manifest.get('id', current_machine_id())}")
    table.add_column("Group")
    table.add_column("State")
    table.add_column("Name")
    if details:
        table.add_column("Version")
        table.add_column("Reference")
    table.add_column("Source")
    for group in groups:
        items = items_by_group[group.key]
        if not items and details:
            row = [group.label, "-", "<empty>"]
            if details:
                row.extend(["", ""])
            table.add_row(*row, group.key)
            continue
        for item in items:
            state = Text("[x]" if item.checked else ("[-]" if item.locked else "[ ]"))
            source = "machine exclusion" if item.managed else ("external exclusion" if item.locked else "included")
            if item.stale:
                source = "stale exclusion"
            row = [group.label, state, item.name]
            if details:
                row.extend([item.version or item.resolved_version or "", item.ref or ""])
            table.add_row(*row, source)
    log.console.print(table)


from envy.sources.cli import app as source_app

app.add_typer(source_app, name="source")
