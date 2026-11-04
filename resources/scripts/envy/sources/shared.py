"""Recoverable registration of shared, locked Nix software inputs."""
from __future__ import annotations

import json
import re

from envy.process import prime_flake_sources, run_process
from envy.secure_io import atomic_write_text
from envy.sources.model import SourceError, public_url
from envy.transaction import FileTransaction
from envy.utils import ENVY_ROOT
from envy.workflows.check import check_or_exit


def managed_block(text, marker):
    matches = list(re.finditer(r"(?ms)^([ \t]*)# BEGIN ENVY MANAGED " + marker + r"\n(.*?)^[ \t]*# END ENVY MANAGED " + marker, text))
    if len(matches) != 1:
        raise SourceError(f"expected one shared managed {marker.lower()} block")
    return matches[0]


def preview(input_name, ref, action, root=ENVY_ROOT):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", input_name):
        raise SourceError("invalid flake input name")
    path = root / "flake.nix"
    text = path.read_text()
    inputs = managed_block(text, "INPUTS")
    bindings = managed_block(text, "SOURCE BINDINGS")
    quoted = json.dumps(input_name)
    if action == "add":
        if not ref:
            raise SourceError("--ref is required when registering a new Nix input")
        if ref.startswith("https:"):
            public_url(ref)
        elif not re.fullmatch(r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?", ref):
            raise SourceError("flake reference must be a public HTTPS or GitHub reference")
        if "${" in ref:
            raise SourceError("invalid flake reference")
        if re.search(r'(?:\b' + re.escape(input_name) + r'\b|' + re.escape(quoted) + r')\s*(?:\.url|=)', text[:inputs.start()] + text[inputs.end():]) or quoted + ".url" in inputs[2]:
            raise SourceError("input is already registered; update it through envy update inputs")
        added_input = f"    {quoted}.url = {json.dumps(ref)};\n    {quoted}.inputs.nixpkgs.follows = \"nixpkgs\";\n"
        added_binding = f"        {quoted} = {{ input = {quoted}; output = \"packages\"; }};\n"
        text = text[:bindings.start(2)] + bindings[2] + added_binding + text[bindings.end(2):]
        inputs = managed_block(text, "INPUTS")
        text = text[:inputs.start(2)] + inputs[2] + added_input + text[inputs.end(2):]
    else:
        if quoted + ".url" not in inputs[2]:
            raise SourceError("only envY-managed inputs can be removed", "source-in-use")
        unmanaged = text[:inputs.start()] + text[inputs.end():bindings.start()] + text[bindings.end():]
        if re.search(r'inputs\.' + re.escape(input_name) + r'\b|follows\s*=\s*"' + re.escape(input_name) + r'"', unmanaged):
            raise SourceError("input is used by handwritten flake outputs or follows", "source-in-use")
        # Reject textual consumers in all versioned Nix modules, not merely selected host.
        for directory in ("hosts", "modules"):
            for consumer in (root / directory).rglob("*.nix"):
                if re.search(r'(?:inputs\.' + re.escape(input_name) + r'\b|flake:' + re.escape(input_name) + r'#|nix:' + re.escape(input_name) + r'["\s])', consumer.read_text()):
                    raise SourceError(f"input is used by {consumer.relative_to(root)}", "source-in-use")
        text = text[:bindings.start(2)] + re.sub(r'(?m)^\s*' + re.escape(quoted) + r' = .*\n', "", bindings[2]) + text[bindings.end(2):]
        inputs = managed_block(text, "INPUTS")
        text = text[:inputs.start(2)] + re.sub(r'(?m)^\s*' + re.escape(quoted) + r'\..*\n', "", inputs[2]) + text[inputs.end(2):]
    return text


def apply(input_name, ref, action, *, root=ENVY_ROOT):
    path = root / "flake.nix"
    original = path.read_text()
    updated = preview(input_name, ref, action, root)
    stage = "locking input"
    try:
        with FileTransaction([path, root / "flake.lock"]) as transaction:
            if path.read_text() != original:
                raise SourceError("flake changed while registering input", "concurrent-edit")
            atomic_write_text(path, updated)
            run_process(["nix", "flake", "lock", f"path:{root}"], cwd=root, check=True,
                        capture=True,
                        activity="shared software source lock")
            prime_flake_sources(root)
            stage = "evaluating all machines"
            check_or_exit(all_machines=True)
            # A source can support either project platform; absent outputs stay unsupported.
            if action == "add":
                supported = []
                for system in ("aarch64-darwin", "x86_64-linux"):
                    stage = f"checking package outputs for {system}"
                    result = run_process(["nix", "eval", "--json",
                        f"path:{root}#softwareSourcePackages.{system}.{input_name}",
                        "--apply", 'ps: builtins.any (p: builtins.isAttrs p && p ? drvPath) (builtins.attrValues ps)'],
                        capture=True, check=True, timeout=120)
                    if result.stdout.strip() == "true":
                        supported.append(system)
                if not supported:
                    raise SourceError("source has no packages on the supported project systems", "shared-validation-failed")
            transaction.commit()
    except (Exception, SystemExit) as exc:
        from envy.evaluation import invalidate_machine_manifest
        invalidate_machine_manifest()
        if isinstance(exc, SourceError):
            raise
        raise SourceError(f"shared source validation failed while {stage}; original flake and lock restored",
                          "shared-validation-failed") from exc
    from envy.evaluation import invalidate_machine_manifest
    invalidate_machine_manifest()
