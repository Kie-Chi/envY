"""Search provider implementations with no policy or rendering."""

import html
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from envy.process import run_process
from envy.search.model import ProviderReport, ResolveResult, SearchResult
from envy.utils import platform_name


Provider = Callable[[str, int, float], ProviderReport]


def resolve_exact(
    ecosystem: str,
    kind: str,
    value: str,
    timeout: float = 10,
) -> ResolveResult:
    """Resolve one exact registry object without accepting fuzzy search matches."""
    name = _name_from_ref(ecosystem, kind, value)
    if not name:
        return ResolveResult.not_found("empty registry name")
    if ecosystem == "homebrew":
        return resolve_homebrew(kind, name, timeout)
    if ecosystem == "npm":
        return resolve_npm(name, timeout)
    if ecosystem == "pypi":
        return resolve_pypi(name, timeout)
    if ecosystem == "native":
        return resolve_native(name, timeout)
    return ResolveResult.unavailable(f"no exact resolver for ecosystem: {ecosystem}")


def available_providers() -> dict[str, Provider]:
    providers: dict[str, Provider] = {
        "pypi": search_pypi,
        "go": search_go,
    }
    if shutil.which("nix"):
        providers["nix"] = search_nix
    if shutil.which("npm"):
        providers["npm"] = search_npm
    if shutil.which("cargo"):
        providers["cargo"] = search_cargo
    if platform_name() == "darwin" and shutil.which("brew"):
        providers["homebrew"] = search_homebrew
    if platform_name() == "linux" and _native_manager() is not None:
        providers["native"] = search_native
    return providers


def search_nix(query: str, limit: int, timeout: float) -> ProviderReport:
    from envy.evaluation import machine_manifest
    from envy.sources.catalog import source_for
    from envy.sources.adapters import search
    manifest = machine_manifest()
    return search(source_for("nix:nixpkgs", manifest), manifest, query, limit, timeout)


def search_homebrew(query: str, limit: int, timeout: float) -> ProviderReport:
    rows: list[SearchResult] = []
    errors = []
    for flag, kind, ref_kind in (
        ("--formula", "formula", "formula"),
        ("--cask", "cask", "cask"),
    ):
        result = run_process(
            ["brew", "search", flag, query],
            capture=True, check=False, timeout=timeout,
        )
        if result.returncode != 0:
            errors.append((result.stderr or "search failed").strip())
            continue
        for name in (result.stdout or "").split():
            rows.append(SearchResult(
                source="homebrew",
                ecosystem="homebrew",
                name=name,
                kind=kind,
                ref=f"homebrew:{ref_kind}/{name}",
            ))
    if not rows and errors:
        return _failure("homebrew", errors[0])
    return ProviderReport("homebrew", rows[:limit])


def resolve_homebrew(kind: str, name: str, timeout: float) -> ResolveResult:
    if kind == "repository":
        result = run_process(
            ["brew", "tap-info", "--json", name],
            capture=True, check=False, timeout=timeout,
        )
        if result.returncode != 0:
            return _command_resolve_failure(result.stderr)
        try:
            values = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            return ResolveResult.unavailable(str(exc))
        entries = values if isinstance(values, list) else [values]
        match = next((
            item for item in entries
            if isinstance(item, dict) and item.get("name") == name
        ), None)
        if match is None:
            return ResolveResult.not_found()
        return ResolveResult.found(SearchResult(
            source="homebrew", ecosystem="homebrew", name=name,
            kind="repository", ref=f"homebrew:tap/{name}", source_id=f"homebrew:{name}",
        ))

    if kind not in {"formula", "cask"}:
        return ResolveResult.unavailable(f"unsupported Homebrew kind: {kind}")
    flag = "--formula" if kind == "formula" else "--cask"
    result = run_process(
        ["brew", "info", "--json=v2", flag, name],
        capture=True, check=False, timeout=timeout,
    )
    if result.returncode != 0:
        return _command_resolve_failure(result.stderr)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return ResolveResult.unavailable(str(exc))
    key = "formulae" if kind == "formula" else "casks"
    entries = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return ResolveResult.not_found()
    item = entries[0]
    resolved_name = _text(
        item.get("token") if kind == "cask" else item.get("name")
    )
    full_name = item.get("full_name") or item.get("full_token")
    if resolved_name != name and full_name != name:
        return ResolveResult.not_found()
    resolved_name = name
    versions = item.get("versions") if isinstance(item.get("versions"), dict) else {}
    version = _text(item.get("version") or versions.get("stable"))
    description = _text(item.get("desc")) or ""
    homepage = _text(item.get("homepage"))
    return ResolveResult.found(SearchResult(
        source="homebrew", ecosystem="homebrew", name=resolved_name,
        kind=kind, version=version, description=description,
        ref=f"homebrew:{kind}/{resolved_name}", homepage=homepage,
        source_id=("homebrew:" + item["tap"]) if isinstance(item.get("tap"), str) else None,
    ))


def parse_native_search(manager: str, output: str) -> list[SearchResult]:
    """Parse each manager's C-locale format without guessing identities."""
    rows = []
    for line in output.splitlines():
        name = version = description = None
        if manager == "apt":
            match = re.match(r"^(\S+) - (.*)$", line)
            if match:
                name, description = match.groups()
        elif manager == "pacman":
            match = re.match(r"^([^/\s]+)/([^\s]+)\s+(\S+)", line)
            if match:
                _, name, version = match.groups()
            elif rows and line.startswith(" "):
                rows[-1].description += (" " if rows[-1].description else "") + line.strip()
        elif manager == "zypper":
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) >= 4 and cells[3] == "package" and cells[1] != "Name":
                name, description = cells[1], cells[2]
        elif manager == "dnf":
            match = re.match(r"^([^\s:]+)\s*:\s*(.+)$", line.strip())
            if match is None:
                match = re.match(r"^([^\s]+\.(?:x86_64|aarch64|noarch|i[3-6]86|ppc64le|s390x))\s+(.+)$", line.strip())
            if match:
                name, description = match.groups()
                name = re.sub(r"\.(?:x86_64|aarch64|noarch|i[3-6]86|ppc64le|s390x)$", "", name)
        if name and valid_native_name(name):
            rows.append(SearchResult(source=manager, ecosystem="native", name=name,
                                     kind="package", version=version, description=description or "",
                                     ref=f"native:{name}"))
    return rows


def valid_native_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*(?::[A-Za-z0-9_-]+)?", name))


def search_native(query: str, limit: int, timeout: float) -> ProviderReport:
    manager = _native_manager()
    commands = {"apt": ["apt-cache", "search", "--names-only", query],
                "pacman": ["pacman", "-Ss", "--", query],
                "dnf": ["dnf", "search", "--", query],
                "zypper": ["zypper", "--non-interactive", "search", "--", query]}
    if manager is None:
        return _failure("native", "no supported native package manager found")
    result = run_process(commands[manager], capture=True, check=False, timeout=timeout,
                         env={**os.environ, "LC_ALL": "C"})
    if result.returncode not in (0, 1) or (result.returncode and not result.stdout):
        return _failure(manager, result.stderr)
    return ProviderReport(manager, parse_native_search(manager, result.stdout or "")[:limit])


def resolve_native(name: str, timeout: float) -> ResolveResult:
    if not valid_native_name(name):
        return ResolveResult.not_found("invalid exact native package name")
    manager = _native_manager()
    commands = {"apt": ["apt-cache", "show", name], "pacman": ["pacman", "-Si", name],
                "dnf": ["dnf", "info", "--", name],
                "zypper": ["zypper", "--non-interactive", "info", "--", name]}
    if manager is None:
        return ResolveResult.unavailable("no supported native package manager found")
    result = run_process(commands[manager], capture=True, check=False, timeout=timeout,
                         env={**os.environ, "LC_ALL": "C"})
    if result.returncode or not (result.stdout or "").strip():
        return _command_resolve_failure(result.stderr or result.stdout)
    for block in re.split(r"\n\s*\n", result.stdout or ""):
        fields = dict((key.casefold(), value.strip()) for key, value in
                      re.findall(r"(?m)^([A-Za-z]+)\s*:\s*(.*)$", block))
        actual = fields.get("package" if manager == "apt" else "name")
        if actual == name.split(":", 1)[0]:
            arch = name.split(":", 1)[1] if ":" in name else None
            if arch and fields.get("architecture") != arch:
                continue
            return ResolveResult.found(SearchResult(source=manager, ecosystem="native", name=name,
                kind="package", version=fields.get("version"), ref=f"native:{name}"))
    return ResolveResult.not_found("manager returned no matching canonical package identity")


def search_npm(query: str, limit: int, timeout: float) -> ProviderReport:
    result = run_process(
        ["npm", "search", query, "--json", f"--searchlimit={limit}"],
        capture=True, check=False, timeout=timeout,
    )
    if result.returncode != 0:
        return _failure("npm", result.stderr)
    try:
        values = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return _failure("npm", str(exc))
    rows = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        publisher = item.get("publisher") if isinstance(item.get("publisher"), dict) else {}
        rows.append(SearchResult(
            source="npm", ecosystem="npm", name=item["name"], kind="tool",
            version=_text(item.get("version")),
            description=_text(item.get("description")) or "",
            ref=f"npm:{item['name']}",
            homepage=_text(links.get("homepage") or links.get("repository")),
            publisher=_text(publisher.get("username")),
        ))
    return ProviderReport("npm", rows[:limit])


def resolve_npm(name: str, timeout: float) -> ResolveResult:
    result = run_process(
        ["npm", "view", name, "--json"],
        capture=True, check=False, timeout=timeout,
    )
    if result.returncode != 0:
        return _command_resolve_failure(result.stderr)
    try:
        item = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return ResolveResult.unavailable(str(exc))
    if not isinstance(item, dict):
        return ResolveResult.not_found()
    resolved_name = _text(item.get("name"))
    if resolved_name != name:
        return ResolveResult.not_found()
    repository = item.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url")
    return ResolveResult.found(SearchResult(
        source="npm", ecosystem="npm", name=resolved_name, kind="tool",
        version=_text(item.get("version")),
        description=_text(item.get("description")) or "",
        ref=f"npm:{resolved_name}",
        homepage=_text(item.get("homepage") or repository),
    ))


def search_pypi(query: str, limit: int, timeout: float) -> ProviderReport:
    del limit
    url = f"https://pypi.org/pypi/{urllib.parse.quote(query, safe='')}/json"
    try:
        payload = _get_json(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ProviderReport("pypi", [])
        return _failure("pypi", str(exc))
    except (OSError, ValueError) as exc:
        return _failure("pypi", str(exc))
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get("name"), str):
        return ProviderReport("pypi", [])
    row = SearchResult(
        source="pypi", ecosystem="pypi", name=info["name"], kind="tool",
        version=_text(info.get("version")),
        description=_text(info.get("summary")) or "",
        ref=f"pypi:{info['name']}", homepage=_text(info.get("project_url") or info.get("home_page")),
        publisher=_text(info.get("author")),
    )
    return ProviderReport("pypi", [row])


def resolve_pypi(name: str, timeout: float) -> ResolveResult:
    report = search_pypi(name, 1, timeout)
    if report.error:
        return ResolveResult.unavailable(report.error)
    match = next((
        item for item in report.results
        if re.sub(r"[-_.]+", "-", item.name).casefold()
        == re.sub(r"[-_.]+", "-", name).casefold()
    ), None)
    return ResolveResult.found(match) if match else ResolveResult.not_found()


def search_cargo(query: str, limit: int, timeout: float) -> ProviderReport:
    result = run_process(
        ["cargo", "search", query, "--limit", str(limit)],
        capture=True, check=False, timeout=timeout,
    )
    if result.returncode != 0:
        return _failure("cargo", result.stderr)
    rows = []
    pattern = re.compile(r'^([^\s=]+)\s*=\s*"([^"]+)"\s*(?:#\s*(.*))?$')
    for line in (result.stdout or "").splitlines():
        match = pattern.match(line.strip())
        if match:
            rows.append(SearchResult(
                source="cargo", ecosystem="cargo", name=match.group(1),
                kind="tool", version=match.group(2), description=match.group(3) or "",
                ref=f"cargo:{match.group(1)}",
            ))
    return ProviderReport("cargo", rows[:limit])


def search_go(query: str, limit: int, timeout: float) -> ProviderReport:
    url = "https://pkg.go.dev/search?" + urllib.parse.urlencode({"q": query, "limit": limit})
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "envy-software/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        return _failure("go", str(exc))
    paths = re.findall(r'href="/([^"?#]+)"[^>]*data-test-id="UnitHeader-path"', body)
    if not paths:
        paths = re.findall(r'data-test-id="UnitHeader-path"[^>]*>\s*<[^>]+>([^<]+)', body)
    rows = []
    for raw in dict.fromkeys(paths):
        name = html.unescape(raw).strip().lstrip("/")
        if name and "." in name:
            rows.append(SearchResult(
                source="go", ecosystem="go", name=name, kind="tool",
                ref=f"go:{name}", homepage=f"https://pkg.go.dev/{name}",
            ))
    return ProviderReport("go", rows[:limit])


def _native_manager() -> str | None:
    for manager, command in (
        ("apt", "apt-cache"), ("dnf", "dnf"), ("pacman", "pacman"), ("zypper", "zypper"),
    ):
        if shutil.which(command):
            return manager
    return None


def _get_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "envy-software/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _failure(source: str, error: object) -> ProviderReport:
    message = str(error or "search failed").strip().splitlines()[-1]
    return ProviderReport(source, [], message[:500])


def _command_resolve_failure(error: object) -> ResolveResult:
    raw = str(error or "registry lookup failed").strip()
    message = raw.splitlines()[-1][:500]
    lowered = raw.casefold()
    not_found_markers = (
        "not found", "no available formula", "no cask with this name",
        "e404", "is not in this registry", "unable to locate package",
        "no matching packages", "target not found", "no packages found",
    )
    if any(marker in lowered for marker in not_found_markers):
        return ResolveResult.not_found(message)
    return ResolveResult.unavailable(message)


def _name_from_ref(ecosystem: str, kind: str, value: str) -> str:
    prefixes = {
        ("homebrew", "formula"): "homebrew:formula/",
        ("homebrew", "cask"): "homebrew:cask/",
        ("homebrew", "repository"): "homebrew:tap/",
        ("npm", "tool"): "npm:",
        ("pypi", "tool"): "pypi:",
        ("native", "package"): "native:",
    }
    prefix = prefixes.get((ecosystem, kind))
    if prefix and value.startswith(prefix):
        return value.removeprefix(prefix)
    return value if ":" not in value else ""


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _homepage(value: object) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return _text(value)
