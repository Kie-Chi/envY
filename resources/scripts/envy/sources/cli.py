"""Unified source CLI; shared registration and machine selection remain distinct."""
from __future__ import annotations

import json
import contextlib
import subprocess
import sys
from pathlib import Path

import typer

from envy import log
from envy.evaluation import machine_manifest, invalidate_machine_manifest
from envy.jsonio import emit, emit_error
from envy.secure_io import atomic_write_text
from envy.sources.catalog import catalog
from envy.sources.completion import complete_ecosystems, complete_source_ids
from envy.sources.model import Source, SourceError
from envy.sources.policy import parse_definitions, render_definitions, source_dependents
from envy.transaction import FileTransaction
from envy.utils import ENVY_ROOT

app = typer.Typer(help="Inspect and manage concrete software sources.", no_args_is_help=True)


def fail(command, exc, json_output):
    if json_output:
        emit_error(command, str(exc), code=getattr(exc, "code", "source-unavailable"))
    else:
        log.error("source", str(exc))
    raise typer.Exit(1) from exc


def snapshot(command=None, json_output=False):
    try:
        manifest = machine_manifest()
        if not manifest:
            raise SourceError("machine manifest is unavailable", "manifest-unavailable")
        return manifest, catalog(manifest)
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if command:
            fail(command, exc, json_output)
        raise


def display(command, data, json_output):
    if json_output:
        emit(command, data=data)
    elif "sources" in data:
        from rich.table import Table
        table = Table(title="Software sources")
        for label in ("Source", "Type", "Registered", "Selected", "Ready", "Owner"):
            table.add_column(label)
        for source in data["sources"]:
            table.add_row(source["id"], source["type"], str(source["registered"]),
                          str(source["selected"]), str(source["ready"]), source["owner"])
        log.console.print(table)
    else:
        log.console.print_json(json.dumps(data, ensure_ascii=False))


@app.command("ls")
def ls(json_output: bool = typer.Option(False, "--json")):
    manifest, sources = snapshot("software.source.ls", json_output)
    display("software.source.ls", {"machine": manifest["id"], "sources": [s.to_dict() for s in sources.values()]}, json_output)


@app.command("show")
def show(source_id: str = typer.Argument(..., autocompletion=complete_source_ids),
         json_output: bool = typer.Option(False, "--json")):
    _, sources = snapshot("software.source.show", json_output)
    if source_id not in sources:
        fail("software.source.show", SourceError("unknown source", "source-not-found"), json_output)
    display("software.source.show", {"source": sources[source_id].to_dict()}, json_output)


@app.command("search")
def search(query: str, ecosystem: str | None = typer.Option(None, autocompletion=complete_ecosystems),
           json_output: bool = typer.Option(False, "--json")):
    _, sources = snapshot("software.source.search", json_output)
    found = [s.to_dict() for s in sources.values()
             if query.lower() in (s.id + " " + s.label).lower() and (ecosystem is None or s.ecosystem == ecosystem)]
    display("software.source.search", {"sources": found}, json_output)


def mutate(source_id, action, definition, ref, dry_run, yes, json_output):
    command = "software.source." + action
    from envy.software import machine_file, _apply_desired_state
    try:
        manifest, sources = snapshot()
        target = machine_file()
        definitions = parse_definitions(target.read_text())
        source = sources.get(source_id)
        custom = None
        if definition:
            custom = json.loads(Path(definition).read_text())
            if not isinstance(custom, dict):
                raise SourceError("source definition must be a JSON object")
            custom["owner"] = "machine"
            source = Source.from_dict(source_id, custom)
            if source_id in sources and sources[source_id].parameters != source.parameters:
                raise SourceError("source ID is already bound; choose a new ID", "source-conflict")
            if source.type == "flake":
                raise SourceError("Nix inputs require shared registration with --ref")
        if source_id.startswith("nix:") and (ref or (source and source.type == "flake")):
            from envy.sources.shared import preview, apply
            input_name = source_id.split(":", 1)[1]
            if action == "add" and source and source.registered and not ref:
                display(command, {"result": "dry-run" if dry_run else "already-satisfied",
                                  "plan": {"sourceId": source_id, "scope": "shared-project", "changed": False, "files": [], "lockUpdate": False}}, json_output)
                return
            updated = preview(input_name, ref, action)
            plan = {"sourceId": source_id, "scope": "shared-project", "files": ["flake.nix", "flake.lock"],
                    "validation": "all-machines", "lockUpdate": True, "changed": True, "input": input_name, "ref": ref}
            import difflib
            plan["diff"] = "".join(difflib.unified_diff((ENVY_ROOT / "flake.nix").read_text().splitlines(True),
                                                       updated.splitlines(True), fromfile="flake.nix", tofile="flake.nix"))
            if dry_run:
                display(command, {"result": "dry-run", "plan": plan}, json_output)
                return
            if json_output and not yes:
                raise SourceError("pass --yes to apply shared registration", "confirmation-required")
            if not yes and not typer.confirm("Apply shared source registration and validate all machines?"):
                return
            with contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext():
                apply(input_name, ref, action)
            display(command, {"result": "applied", "plan": plan}, json_output)
            return
        if not source:
            raise SourceError("unknown source; supply --definition for a custom source", "source-not-found")
        dependencies = source_dependents(source_id, manifest)
        if action == "rm" and dependencies:
            raise SourceError("source is required by: " + ", ".join(d["item"] for d in dependencies), "source-in-use")
        if source.type in ("native", "tap"):
            group = "native.system.repository" if source.type == "native" else "homebrew.system.repository"
            item = source_id if source.type == "native" else source.parameters["tap"]
            if source.parameters.get("system"):
                raise SourceError("system source set is observed, not an editable repository", "unsupported")
            if not custom:
                _apply_desired_state(group, item, action=action, clean=False, yes=yes,
                                     dry_run=dry_run, explicit_ref=None, json_output=json_output, source_id=source_id, command_prefix="software.source")
                return
        elif source.type not in ("registry", "artifact"):
            raise SourceError("source selection is unsupported", "unsupported")
        if action == "add":
            definitions[source_id] = custom or {key: value for key, value in source.to_dict().items()
                                               if key in ("label", "ecosystem", "type", "platforms", "scope", "parameters", "owner")}
        else:
            if source_id not in definitions:
                raise SourceError("shared module source definitions cannot be removed", "source-in-use")
            del definitions[source_id]
        plan = {"sourceId": source_id, "scope": "machine", "files": [str(target)],
                "sourceDefinition": action, "changed": True}
        # Prepare one complete host result, including its source selection.
        from envy.software import _parse_managed_policy_source, _update_managed_policy_source, groups_for_manifest, ManagedInclude
        original = target.read_text()
        updated = render_definitions(original, definitions)
        groups = groups_for_manifest(manifest, include_empty=True)
        if source.type in ("native", "tap") and action == "add":
            includes, exclusions = _parse_managed_policy_source(updated, groups)
            item_id = source_id.split(":", 1)[1]
            if item_id in exclusions.get(group, []):
                raise SourceError("source is explicitly excluded", "source-excluded")
            if not any(i.group == group and i.id == item_id for i in includes):
                includes.append(ManagedInclude(group, item_id, item_id, ref=source_id, parameters={}, source_id=source_id))
            updated = _update_managed_policy_source(updated, includes, exclusions, groups)
            plan["selection"] = {"group": group, "item": item_id}
        import difflib
        plan["diff"] = "".join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                                                   fromfile=str(target), tofile=str(target)))
        plan["changed"] = original != updated
        if dry_run:
            display(command, {"result": "dry-run", "plan": plan}, json_output)
            return
        if json_output and not yes:
            raise SourceError("pass --yes to apply source definition", "confirmation-required")
        if not yes and not typer.confirm("Apply source definition and selection?"):
            return
        with FileTransaction([target]) as transaction:
            if target.read_text() != original:
                raise SourceError("machine changed while planning", "concurrent-edit")
            atomic_write_text(target, updated)
            evaluated = machine_manifest(refresh=True)
            if not evaluated or (action == "add" and source_id not in evaluated.get("software", {}).get("sources", {})):
                invalidate_machine_manifest()
                raise SourceError("source verification failed; changes restored", "validation-failed")
            transaction.commit()
        display(command, {"result": "applied", "plan": plan}, json_output)
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if json_output:
            emit_error(command, str(exc), code=getattr(exc, "code", "invalid-source"))
        else:
            log.error("source", str(exc))
        raise typer.Exit(1) from exc


@app.command("add")
def add(source_id: str = typer.Argument(..., autocompletion=complete_source_ids),
        definition: str | None = None, ref: str | None = None,
        dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y"),
        json_output: bool = typer.Option(False, "--json")):
    mutate(source_id, "add", definition, ref, dry_run, yes, json_output)


@app.command("rm")
def rm(source_id: str = typer.Argument(..., autocompletion=complete_source_ids),
       dry_run: bool = typer.Option(False, "--dry-run"),
       yes: bool = typer.Option(False, "--yes", "-y"), json_output: bool = typer.Option(False, "--json")):
    mutate(source_id, "rm", None, None, dry_run, yes, json_output)


@app.command("sync")
def sync(source_id: str = typer.Argument(..., autocompletion=complete_source_ids),
         dry_run: bool = typer.Option(False, "--dry-run"),
         yes: bool = typer.Option(False, "--yes", "-y"), json_output: bool = typer.Option(False, "--json")):
    command = "software.source.sync"
    try:
        manifest, sources = snapshot()
        source = sources.get(source_id)
        if not source:
            raise SourceError("unknown source", "source-not-found")
        scope = "all-homebrew-taps" if source.type == "tap" else "envy-private-metadata"
        plan = {"sourceId": source_id, "scope": scope, "lockUpdate": False, "systemPackageChanges": False}
        if dry_run:
            display(command, {"result": "dry-run", "plan": plan}, json_output)
            return
        if json_output and not yes:
            raise SourceError("pass --yes for metadata synchronization", "confirmation-required")
        if not yes and not typer.confirm(f"Synchronize metadata ({scope})?"):
            return
        from envy.process import run_process
        if source.type == "tap":
            run_process(["brew", "update"], check=True)
        elif source.type == "native":
            from envy.sources.native.refresh import refresh
            refresh(source)
        elif source.type == "flake":
            run_process(["nix", "flake", "metadata", "--json", "--no-update-lock-file", f"path:{ENVY_ROOT}"],
                        capture=True, check=True, timeout=120)
        elif not source.capabilities.get("indexRefresh"):
            raise SourceError("source has no metadata synchronization capability", "unsupported")
        from envy.search.index import RegistryIndex
        RegistryIndex().clear_source(source_id)
        from envy.search.runner import invalidate_source_cache
        invalidate_source_cache(source_id)
        display(command, {"result": "applied", "plan": plan}, json_output)
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        fail(command, exc, json_output)
