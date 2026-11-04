"""Read-only adapters bound to concrete source identity and context."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse

from envy.process import run_process
from envy.search.model import ProviderReport, ResolveResult, SearchResult
from envy.sources.catalog import native_manager
from envy.sources.model import Source, SourceError
from envy.utils import ENVY_ROOT


def bind(result: SearchResult, source: Source) -> SearchResult:
    result.source_id = source.id
    result.source_fingerprint = source.fingerprint
    result.resolved_version = result.version
    if source.parameters.get("discoveryOnly"):
        result.availability = "discovery-only"
    elif not source.ready:
        result.availability = "needs-source"
    return result


def nix_target(source: Source, manifest: dict, attr: str = "") -> str:
    if not source.registered:
        raise SourceError("Nix source input is not locked", "source-not-ready")
    if manifest.get("platform", "darwin" if manifest.get("system") == "aarch64-darwin" else "linux") not in source.platforms:
        raise SourceError("Nix source has no package output for this platform", "unsupported-platform")
    if attr and not re.fullmatch(r"[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*", attr):
        raise SourceError("invalid Nix attribute")
    system = manifest.get("system")
    if system not in ("aarch64-darwin", "x86_64-linux"):
        raise SourceError("machine system is required for Nix lookup")
    name = source.parameters["input"]
    return f"path:{ENVY_ROOT}#softwareSourcePackages.{system}.{name}" + (f".{attr}" if attr else "")


def resolve_nix(source: Source, manifest: dict, value: str, timeout=30) -> ResolveResult:
    attr = value.removeprefix("nix:") if value.startswith("nix:") else value.split("#", 1)[-1]
    system = str(manifest.get("system", ""))
    for prefix in (f"legacyPackages.{system}.", f"packages.{system}."):
        attr = attr.removeprefix(prefix)
    try:
        target = nix_target(source, manifest, attr)
    except SourceError as exc:
        return ResolveResult.unavailable(str(exc))
    result = run_process(["nix", "eval", "--json", target, "--no-update-lock-file", "--apply",
        'x: { name = x.pname or (builtins.parseDrvName x.name).name; version = x.version or (builtins.parseDrvName x.name).version; drvPath = x.drvPath; }'],
        capture=True, check=False, timeout=timeout)
    if result.returncode:
        if re.search(r"(?:attribute '[^']+' missing|does not provide attribute)", result.stderr or ""):
            return ResolveResult.not_found("locked Nix source has no such attribute")
        return ResolveResult.unavailable("locked Nix attribute evaluation failed")
    try:
        item = json.loads(result.stdout)
        if not isinstance(item.get("drvPath"), str) or not item["drvPath"].endswith(".drv"):
            return ResolveResult.not_found("attribute is not a derivation")
        ref = f"nix:{attr}" if source.id == "nix:nixpkgs" else f"flake:{source.parameters['input']}#{attr}"
        return ResolveResult.found(bind(SearchResult("nix", "nix", item["name"], "package",
            version=item.get("version"), ref=ref), source))
    except (ValueError, TypeError, KeyError):
        return ResolveResult.unavailable("invalid Nix package metadata")


@contextlib.contextmanager
def apt_context(source: Source):
    """Unconfigured vendor lookup uses private signed lists, never the system DB."""
    from envy.sources.native.apt import effective_options
    with effective_options() as options:
        if source.parameters.get("system"):
            yield options
            return
    if native_manager() != "apt":
        raise SourceError("temporary signed repository lookup requires APT", "unsupported")
    from envy.sources.native.apt import signed_options
    with signed_options(source) as options:
        yield options


from envy.sources.native.apt import candidate_from_uri


def resolve_native_source(source: Source, value: str, timeout: float, *, options_override=None) -> ResolveResult:
    from envy.search.providers import resolve_native, valid_native_name, _command_resolve_failure
    name = value.removeprefix("native:")
    if not valid_native_name(name):
        return ResolveResult.not_found("invalid exact package identity")
    if native_manager() != "apt":
        if not source.parameters.get("system"):
            return ResolveResult.unavailable("manager cannot enforce the requested repository constraint")
        outcome = resolve_native(name, timeout)
        if outcome.result:
            bind(outcome.result, source)
        return outcome
    try:
        with (contextlib.nullcontext(options_override) if options_override is not None else apt_context(source)) as options:
            result = run_process(["apt-cache", *options, "policy", name], capture=True, check=False,
                                 timeout=timeout, env={**os.environ, "LC_ALL": "C"})
            if result.returncode:
                return _command_resolve_failure(result.stderr)
            match = re.search(r"(?m)^\s*Candidate:\s*(\S+)", result.stdout or "")
            if not match or match.group(1) == "(none)":
                return ResolveResult.not_found()
            version = match.group(1)
            if not source.parameters.get("system"):
                from envy.sources.catalog import os_release
                from envy.sources.native.apt import expand_value
                import platform
                architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
                uri = expand_value(source.parameters["apt"]["uri"], os_release(), architecture)
                if not candidate_from_uri(result.stdout or "", version, uri):
                    return ResolveResult.unavailable("APT candidate does not satisfy the requested source")
            result = run_process(["apt-cache", *options, "show", f"{name}={version}"],
                                 capture=True, check=False, timeout=timeout, env={**os.environ, "LC_ALL": "C"})
            if result.returncode:
                return _command_resolve_failure(result.stderr)
            identity = re.search(r"(?m)^Package:\s*(\S+)", result.stdout or "")
            if identity is None or identity.group(1) != name.split(":")[0]:
                return ResolveResult.not_found("candidate identity mismatch")
            if ":" in name:
                architecture = re.search(r"(?m)^Architecture:\s*(\S+)", result.stdout or "")
                if architecture is None or architecture[1] != name.split(":", 1)[1]:
                    return ResolveResult.not_found("candidate architecture mismatch")
            return ResolveResult.found(bind(SearchResult("apt", "native", name, "package",
                                                         version=version, ref=f"native:{name}"), source))
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return ResolveResult.unavailable(str(exc))


def npm_endpoint(source):
    if source.id == "npm:npmjs":
        return source.context.get("transport", {}).get("registry", source.parameters["registry"])
    return source.parameters["registry"]


def resolve(source: Source, manifest: dict, kind: str, value: str, timeout=30) -> ResolveResult:
    from envy.search import providers
    if source.type == "flake":
        return resolve_nix(source, manifest, value, timeout)
    if source.type == "native":
        if kind == "repository":
            return ResolveResult.found(bind(SearchResult("native", "native", source.id.split(":", 1)[1],
                "repository", ref=source.id), source))
        return resolve_native_source(source, value, timeout)
    if source.type == "artifact":
        from envy.sources.artifacts import resolve_artifact
        return resolve_artifact(source, timeout)
    if source.ecosystem == "npm":
        name = value.removeprefix("npm:")
        result = run_process(["npm", "view", name, "--json", "--registry", npm_endpoint(source)],
                             capture=True, check=False, timeout=timeout)
        if result.returncode:
            return providers._command_resolve_failure(result.stderr)
        try:
            data = json.loads(result.stdout)
            if data.get("name") != name:
                return ResolveResult.not_found()
            return ResolveResult.found(bind(SearchResult("npm", "npm", name, kind,
                version=data.get("version"), ref=f"npm:{name}"), source))
        except (ValueError, AttributeError):
            return ResolveResult.unavailable("invalid npm metadata")
    if source.ecosystem == "pypi":
        name = value.removeprefix("pypi:")
        try:
            data = providers._get_json(source.parameters["metadata"].rstrip("/") + "/" +
                                       urllib.parse.quote(name, safe="") + "/json", timeout)["info"]
            if re.sub(r"[-_.]+", "-", data["name"]).lower() != re.sub(r"[-_.]+", "-", name).lower():
                return ResolveResult.not_found()
            return ResolveResult.found(bind(SearchResult("pypi", "pypi", data["name"], kind,
                version=data.get("version"), ref=f"pypi:{data['name']}"), source))
        except urllib.error.HTTPError as exc:
            return ResolveResult.not_found() if exc.code == 404 else ResolveResult.unavailable("PyPI unavailable")
        except (OSError, ValueError, KeyError):
            return ResolveResult.unavailable("PyPI metadata unavailable")
    outcome = providers.resolve_exact(source.ecosystem, kind, value, timeout)
    if outcome.result:
        # Homebrew must use its real tap, including fully qualified names.
        if source.type == "tap" and outcome.result.source_id != source.id:
            return ResolveResult.unavailable("Homebrew object belongs to another tap")
        bind(outcome.result, source)
    return outcome


def search(source: Source, manifest: dict, query: str, limit: int, timeout: float) -> ProviderReport:
    from envy.search import providers
    try:
        if not source.capabilities.get("search"):
            return ProviderReport(source.id, [], "source search is unsupported")
        if source.type == "flake":
            result = run_process(["nix", "search", nix_target(source, manifest).replace("#softwareSourcePackages.", "#legacyPackages."), query, "--json", "--no-update-lock-file"],
                                 capture=True, check=False, timeout=max(timeout, 45))
            if result.returncode:
                return ProviderReport(source.id, [], "locked Nix source search failed")
            rows = []
            for attr, data in json.loads(result.stdout or "{}").items():
                prefix = f"legacyPackages.{manifest['system']}.{source.parameters['input']}."
                attr = attr.removeprefix(prefix)
                rows.append(bind(SearchResult("nix", "nix", data.get("pname", attr.split(".")[-1]), "package",
                    version=data.get("version"), description=data.get("description", ""),
                    ref=f"nix:{attr}" if source.id == "nix:nixpkgs" else f"flake:{source.parameters['input']}#{attr}"), source))
            return ProviderReport(source.id, rows[:limit])
        if source.type == "native" and native_manager() == "apt":
            with apt_context(source) as options:
                result = run_process(["apt-cache", *options, "search", "--names-only", query], capture=True,
                                     check=False, timeout=timeout, env={**os.environ, "LC_ALL": "C"})
                if result.returncode:
                    return ProviderReport(source.id, [], "APT source search failed")
                rows = providers.parse_native_search("apt", result.stdout or "")[:limit]
                if not source.parameters.get("system"):
                    rows = [outcome.result for row in rows
                            if (outcome := resolve_native_source(source, row.name, timeout, options_override=options)).result]
                return ProviderReport(source.id, [bind(row, source) for row in rows])
        if source.ecosystem == "npm":
            result = run_process(["npm", "search", query, "--json", f"--searchlimit={limit}",
                                  "--registry", npm_endpoint(source)], capture=True, check=False, timeout=timeout)
            if result.returncode:
                return ProviderReport(source.id, [], "npm source search failed")
            rows = [bind(SearchResult("npm", "npm", item["name"], "tool", version=item.get("version"),
                    description=item.get("description", ""), ref="npm:" + item["name"]), source)
                    for item in json.loads(result.stdout or "[]") if isinstance(item.get("name"), str)]
            return ProviderReport(source.id, rows[:limit])
        if source.ecosystem == "pypi":
            outcome = resolve(source, manifest, "tool", query, timeout)
            return ProviderReport(source.id, [outcome.result] if outcome.result else [],
                                  outcome.error if outcome.status == "unavailable" else None)
        provider = providers.available_providers().get(source.ecosystem)
        if provider is None:
            return ProviderReport(source.id, [], "source search unsupported on this host")
        report = provider(query, limit, timeout)
        if source.type == "tap":
            rows = []
            for row in report.results:
                outcome = providers.resolve_homebrew(row.kind, row.name, timeout)
                if outcome.result and outcome.result.source_id == source.id:
                    rows.append(bind(outcome.result, source))
            report.results = rows
        else:
            report.results = [bind(row, source) for row in report.results]
        report.source = source.id
        return report
    except (SourceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return ProviderReport(source.id, [], str(exc))
