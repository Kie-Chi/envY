"""Read-only Docker accounting and explicit, revalidated native maintenance targets."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from envy.storage.measurement import ScanBudget
from envy.storage.model import StorageError
from envy.storage.reporting import probe

MAX_RESPONSE = 1_048_576
AGE_SECONDS = 14 * 24 * 60 * 60
LABEL_KEYS = ("com.docker.compose.project", "com.docker.compose.service", "dev.envy.amux",
              "dev.envy.amux.image", "dev.envy.amux.agent")
CONTEXT_FORMAT = "{{json .Endpoints.docker.Host}}"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)\Z")


class _Unavailable(StorageError):
    def __init__(self, reason: str, status: str = "unknown"):
        super().__init__(reason)
        self.status = status


def bound_environment() -> dict[str, str]:
    """Keep context storage but remove ambient engine, TLS and builder selectors."""
    return {key: value for key, value in os.environ.items()
            if key not in {"DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_CERT_PATH", "DOCKER_API_VERSION",
                           "BUILDKIT_HOST"}
            and not key.startswith(("DOCKER_TLS", "BUILDX_", "BUILDKIT_TLS"))}


def _command(argv: list[str], budget: ScanBudget) -> str:
    budget.check()
    result = probe(argv, budget=budget, env=bound_environment(), max_bytes=MAX_RESPONSE)
    if result.returncode:
        operation = "buildx inspect" if "buildx" in argv else "context metadata"
        reason = {124: "Docker probe deadline exhausted", 125: "Docker probe output limit exceeded or Docker rejected the command (exit 125)",
                  127: "Docker command is unavailable"}.get(result.returncode, f"Docker {operation} probe failed (exit {result.returncode})")
        raise _Unavailable(reason, "unavailable" if result.returncode == 127 else "unknown")
    budget.check()
    return result.stdout.strip()


def _decode(value: str):
    try:
        return json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise _Unavailable("Docker returned invalid JSON metadata") from exc


def _name(value) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _Unavailable("Docker context or builder name is invalid")
    return value


def _local_endpoint(value) -> str:
    if not isinstance(value, str):
        raise _Unavailable("Docker endpoint is missing")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _Unavailable("Docker endpoint is invalid") from exc
    if parsed.scheme != "unix":
        # Never echo a remote URL: it may contain credentials.
        raise _Unavailable("Remote Docker endpoints are not inspected or maintained", "remote")
    if (parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith("/")
            or parsed.path.startswith("//") or ".." in parsed.path.split("/")
            or "%" in parsed.path or any(ord(char) <= 32 for char in value)):
        raise _Unavailable("Docker requires an absolute local Unix socket endpoint")
    return "unix://" + parsed.path


def _context_endpoint(context: str, budget: ScanBudget) -> str:
    value = _decode(_command(["docker", "context", "inspect", "--format", CONTEXT_FORMAT,
                              _name(context)], budget))
    return _local_endpoint(value)


def _selection(budget: ScanBudget) -> dict:
    if os.environ.get("DOCKER_CONTEXT"):
        context, source = _name(os.environ["DOCKER_CONTEXT"]), "DOCKER_CONTEXT"
    elif os.environ.get("DOCKER_HOST"):
        return {"source": "DOCKER_HOST", "context": None,
                "endpoint": _local_endpoint(os.environ["DOCKER_HOST"])}
    else:
        context = _name(_command(["docker", "context", "show"], budget))
        source = "current-context"
    return {"source": source, "context": context, "endpoint": _context_endpoint(context, budget)}


class _SocketReader(io.RawIOBase):
    """Apply one absolute deadline to every receive, including slow HTTP headers."""
    def __init__(self, stream, deadline: float, budget: ScanBudget, max_bytes: int):
        self.stream, self.deadline, self.budget = stream, deadline, budget
        self.remaining = max_bytes

    def readable(self):
        return True

    def readinto(self, buffer):
        self.budget.check()
        remaining_time = self.deadline - time.monotonic()
        if remaining_time <= 0:
            raise _Unavailable("Docker HTTP deadline exhausted")
        self.stream.settimeout(remaining_time)
        count = self.stream.recv_into(memoryview(buffer)[:min(len(buffer), self.remaining + 1)])
        self.remaining -= count
        if self.remaining < 0:
            raise _Unavailable("Docker HTTP response exceeds the size limit")
        self.budget.check()
        if time.monotonic() >= self.deadline:
            raise _Unavailable("Docker HTTP deadline exhausted")
        return count


class _HTTPStream:
    def __init__(self, stream, deadline, budget, max_bytes):
        self.reader = io.BufferedReader(_SocketReader(stream, deadline, budget, max_bytes))

    def makefile(self, *args, **kwargs):
        return self.reader


def _get(endpoint: str, path: str, budget: ScanBudget, *, timeout: float = 5,
         max_bytes: int = MAX_RESPONSE) -> dict:
    budget.check()
    endpoint = _local_endpoint(endpoint)
    deadline = min(budget.deadline, time.monotonic() + timeout)
    if deadline <= time.monotonic():
        raise _Unavailable("Docker HTTP deadline exhausted")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(max(0.000001, deadline - time.monotonic()))
            stream.connect(endpoint[len("unix://"):])
            stream.settimeout(max(0.000001, deadline - time.monotonic()))
            stream.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode("ascii"))
            wrapper = _HTTPStream(stream, deadline, budget, max_bytes)
            response = None
            try:
                response = http.client.HTTPResponse(wrapper)
                response.begin()
                if response.status != 200:
                    raise _Unavailable("Docker HTTP metadata request failed")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise _Unavailable("Docker HTTP response exceeds the size limit")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise _Unavailable("Docker HTTP response exceeds the size limit")
                if length is not None and len(payload) != int(length):
                    raise _Unavailable("Docker HTTP response is incomplete")
                budget.check()
                data = _decode(payload.decode("utf-8"))
                budget.check()
                if time.monotonic() >= deadline:
                    raise _Unavailable("Docker HTTP deadline exhausted")
                if not isinstance(data, dict):
                    raise _Unavailable("Docker HTTP metadata must be an object")
                return data
            finally:
                if response is not None:
                    response.close()
                wrapper.reader.close()
    except _Unavailable:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise _Unavailable("Docker local socket metadata is unavailable or incomplete", "unavailable") from exc


def _daemon(endpoint: str, budget: ScanBudget) -> str:
    value = _get(endpoint, "/info", budget).get("ID")
    if not isinstance(value, str) or not value or len(value) > 256:
        raise _Unavailable("Docker daemon identity is unavailable")
    return value


def _builder_metadata(output: str, budget: ScanBudget) -> dict:
    """Read fixed fields from inspect's native text; buildx has no --format flag."""
    value = {"nodes": []}
    current, in_nodes = value, False
    for line in output.splitlines():
        budget.check()
        if line.startswith((" ", "\t")):
            continue
        key, separator, content = line.partition(":")
        if not separator:
            continue
        if key == "Nodes":
            if in_nodes or content.strip():
                raise _Unavailable("Docker buildx inspect returned ambiguous node metadata")
            in_nodes = True
            current = None
            continue
        if in_nodes and key == "Name":
            current = {}
            value["nodes"].append(current)
        allowed = ("Name", "Endpoint", "Status") if in_nodes else ("Name", "Driver")
        if key in allowed:
            if current is None or key.lower() in current:
                raise _Unavailable("Docker buildx inspect returned duplicate or misplaced identity fields")
            current[key.lower()] = content.strip()
    return value


def _builder(target: dict, budget: ScanBudget) -> dict:
    # A named context's built-in docker driver is not the selected buildx builder.
    name = _name(target["context"] or "default")
    value = _builder_metadata(_command(["docker", "--host", target["endpoint"], "buildx", "inspect", name], budget), budget)
    if value.get("name") != name:
        raise _Unavailable("Docker buildx inspect did not confirm the explicitly named builder")
    if value.get("driver") != "docker":
        raise _Unavailable("The named buildx builder does not use the native docker driver")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        raise _Unavailable("The native Docker builder must have exactly one known node")
    node = nodes[0]
    node_name = _name(node.get("name"))
    if node.get("status") != "running":
        raise _Unavailable("The named Docker builder is not running; bootstrap is disabled")
    endpoint = node.get("endpoint")
    if isinstance(endpoint, str) and _NAME.fullmatch(endpoint):
        endpoint = _context_endpoint(endpoint, budget)
    else:
        endpoint = _local_endpoint(endpoint)
    if endpoint != target["endpoint"]:
        raise _Unavailable("The named buildx builder endpoint does not match the Docker engine")
    return {"name": name, "driver": "docker",
            "nodes": [{"name": node_name, "endpoint": endpoint, "status": "running"}]}


def maintenance_target(*, budget: ScanBudget | None = None) -> dict:
    """Resolve and prove a local engine/native builder binding, failing closed."""
    budget = budget or ScanBudget()
    try:
        target = _selection(budget)
        target["daemonId"] = _daemon(target["endpoint"], budget)
        target["builder"] = _builder(target, budget)
        return target
    except StorageError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StorageError("Docker maintenance target could not be verified") from exc


def bound_commands(target: dict) -> list[list[str]]:
    """Only render commands. The caller must verify before each execution."""
    try:
        endpoint = _local_endpoint(target["endpoint"])
        builder = target["builder"]
        name = _name(builder["name"])
        if (not isinstance(target["daemonId"], str) or not target["daemonId"]
                or builder["driver"] != "docker" or len(builder["nodes"]) != 1
                or builder["nodes"][0]["endpoint"] != endpoint):
            raise StorageError("Docker maintenance target is not bound")
    except (KeyError, TypeError, IndexError) as exc:
        raise StorageError("Docker maintenance target is incomplete") from exc
    prefix = ["docker", "--host", endpoint]
    return [[*prefix, "buildx", "prune", "--builder", name, "-f", "--filter", "until=336h"],
            [*prefix, "image", "prune", "-f", "--filter", "until=336h"]]


def verify_target(target: dict) -> None:
    """Recheck selection, endpoint, daemon ID and native builder before each step."""
    bound_commands(target)
    if maintenance_target() != target:
        raise StorageError("Docker context, endpoint, daemon or builder changed; maintenance stopped")


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.year > 1 else None
    except (ValueError, OverflowError, OSError):
        return None


def _created(value):
    if type(value) is int and value > 0:
        try:
            return datetime.fromtimestamp(value, timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return _date(value)


def _ownership(raw: dict) -> dict:
    labels = raw.get("Labels")
    labels = {key: labels[key] for key in LABEL_KEYS if isinstance(labels.get(key), str)} if isinstance(labels, dict) else {}
    amux = labels.get("dev.envy.amux") == "1" or labels.get("dev.envy.amux.image") == "1"
    return {"labels": labels, "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"), "amux": amux,
            "test": None, "completionEvidence": None, "retentionEvidence": None,
            "action": "report"}


def _sum(items: list[dict], key: str):
    values = [entry.get(key) for entry in items]
    return sum(values) if all(value is not None for value in values) else None



def _identifier(value):
    return value if isinstance(value, str) and value and len(value) <= 512 and all(ord(char) > 32 for char in value) else None


def _container_fields(raw: dict, budget: ScanBudget) -> tuple[dict, set[str], bool]:
    """Project mount identity without disclosing bind sources or container commands."""
    mounts, names = [], set()
    metadata_known = names_known = isinstance(raw.get("Mounts"), list)
    for mount in raw.get("Mounts", []) if names_known else []:
        budget.check(entry=True)
        kind = mount.get("Type") if isinstance(mount, dict) else None
        if kind not in ("volume", "bind", "tmpfs", "npipe", "cluster"):
            mounts.append({"type": None})
            metadata_known = names_known = False
            continue
        projected = {"type": kind}
        if kind == "volume":
            name = _identifier(mount.get("Name"))
            destination = mount.get("Destination")
            destination = destination if isinstance(destination, str) and destination.startswith("/") else None
            writable = mount.get("RW") if type(mount.get("RW")) is bool else None
            projected.update(name=name, destination=destination, rw=writable)
            if name is None:
                names_known = False
            else:
                names.add(name)
            if name is None or destination is None or writable is None:
                metadata_known = False
        mounts.append(projected)
    state = raw.get("State")
    state = state if state in ("created", "running", "paused", "restarting", "removing", "exited", "dead") else None
    return ({"imageId": _identifier(raw.get("ImageID")), "state": state,
             "mounts": mounts if isinstance(raw.get("Mounts"), list) else None,
             "mountStatus": "complete" if metadata_known else "unknown",
             "mountReason": None if metadata_known else "Container mount metadata is missing or malformed"},
            names, names_known)


def _associations(report: dict, data: dict, budget: ScanBudget) -> None:
    """Build reverse indices once; unknown inventories never prove empty references."""
    raw = data.get("Containers")
    container_ids, image_refs, volume_refs = set(), {}, {}
    image_known = volume_known = isinstance(raw, list)
    image_reason = volume_reason = "Container inventory is missing or malformed"
    for index, container in enumerate(raw if isinstance(raw, list) else []):
        budget.check(entry=True)
        if not isinstance(container, dict):
            image_known = volume_known = False
            continue
        fields, names, mounts_known = _container_fields(container, budget)
        if fields["state"] is None:
            image_known = volume_known = False
            image_reason = volume_reason = "Container State is missing or malformed"
        if report["containers"]["count"] is not None:
            report["containers"]["items"][index].update(fields)
        identifier = _identifier(container.get("Id"))
        if identifier is None or identifier in container_ids:
            image_known = volume_known = False
            image_reason = volume_reason = "Container identity is missing, malformed or duplicated"
        if identifier is None:
            continue
        container_ids.add(identifier)
        if fields["imageId"] is None:
            image_known = False
            image_reason = "Container ImageID is missing or malformed"
        else:
            image_refs.setdefault(fields["imageId"], set()).add(identifier)
        if not mounts_known:
            volume_known = False
            volume_reason = "Container Mounts or volume Name is missing or malformed"
        for name in names:
            budget.check()
            volume_refs.setdefault(name, set()).add(identifier)
    for kind, references, complete, reason in (("images", image_refs, image_known, image_reason),
                                               ("volumes", volume_refs, volume_known, volume_reason)):
        group = report[kind]
        identities = set()
        source_known = group["count"] is not None
        for entry in group["items"]:
            budget.check()
            identifier = _identifier(entry["id"])
            if identifier is None or identifier in identities:
                source_known = False
            identities.add(identifier)
        if not source_known:
            complete, reason = False, "Source inventory identity is missing, malformed or duplicated"
        for identifier in references:
            budget.check()
            if identifier not in identities:
                complete, reason = False, "Container reference is absent from the source inventory"
        group_complete = complete
        group_reason = None if complete else reason
        for entry in group["items"]:
            budget.check()
            observed = sorted(references.get(_identifier(entry["id"]), ()))
            count = entry["references"]
            entry_complete, entry_reason = complete, None if complete else reason
            if count is None:
                entry_complete, entry_reason = False, "Native reference count is missing or malformed"
            elif count < len(observed) or (complete and count != len(observed)):
                entry_complete, entry_reason = False, "Native reference count disagrees with the observed container set"
            entry["association"] = {"status": "complete" if entry_complete else "unknown",
                                    "referencedBy": observed if entry_complete else None,
                                    "observedReferencedBy": observed, "reason": entry_reason}
            if not entry_complete:
                group_complete, group_reason = False, entry_reason
        group.update(associationStatus="complete" if group_complete else "unknown", associationReason=group_reason)
    container_complete = all(report[kind]["associationStatus"] == "complete" for kind in ("images", "volumes"))
    report["containers"].update(associationStatus="complete" if container_complete else "unknown",
                                 associationReason=None if container_complete else "Container references or source evidence are incomplete")


def _group(raw, kind: str, now: float, builder_known: bool, budget: ScanBudget) -> dict:
    empty = {"count": None, "logicalBytes": None, "nativeReclaimableBytes": None,
             "policyEligibleBytes": None, "items": [], "action": "report", "totalsAdditive": False}
    if kind in ("images", "volumes", "containers"):
        empty.update(associationStatus="unknown", associationReason="Association evidence is unavailable")
    if not isinstance(raw, list):
        return empty
    items = []
    for value in raw:
        budget.check(entry=True)
        if not isinstance(value, dict):
            return empty
        size = _integer(value.get("Size"))
        created = _created(value.get("CreatedAt", value.get("Created")))
        entry = {"id": value.get("ID", value.get("Id", value.get("Name"))), "action": "report",
                 "logicalBytes": size, "nativeReclaimableBytes": None, "policyEligibleBytes": None,
                 "createdAt": created, "ownership": _ownership(value),
                 "note": "Ownership labels are not completion or deletion authorization."}
        if kind == "images":
            refs = _integer(value.get("Containers"))
            shared, unique = _integer(value.get("SharedSize")), _integer(value.get("UniqueSize"))
            if value.get("UniqueSize") is None and size is not None and shared is not None and shared <= size:
                unique = size - shared
            tags = value.get("RepoTags")
            dangling = (all(tag == "<none>:<none>" for tag in tags)
                        if isinstance(tags, list) else None)
            entry.update(references=refs, sharedBytes=shared, uniqueBytes=unique, dangling=dangling)
            if (size is not None and refs is not None and unique is not None
                    and shared is not None and unique + shared <= size):
                entry["nativeReclaimableBytes"] = unique if refs == 0 else 0
            if dangling is False or (refs is not None and refs > 0) or (created is not None and created >= now - AGE_SECONDS):
                entry["policyEligibleBytes"] = 0
            elif (refs == 0 and dangling is True and created is not None
                  and entry["nativeReclaimableBytes"] is not None):
                entry["policyEligibleBytes"] = entry["nativeReclaimableBytes"]
        elif kind == "buildCache":
            used, shared = value.get("InUse"), value.get("Shared")
            last = _date(value.get("LastUsedAt"))
            entry.update(inUse=used if type(used) is bool else None,
                         shared=shared if type(shared) is bool else None, lastUsedAt=last)
            if size is not None and type(used) is bool and type(shared) is bool:
                entry["nativeReclaimableBytes"] = 0 if used or shared else size
            if builder_known:
                if (used is True or shared is True or value.get("Type") in ("internal", "frontend")
                        or (last is not None and last >= now - AGE_SECONDS)):
                    entry["policyEligibleBytes"] = 0
                elif (last is not None and value.get("Type") == "regular"
                      and type(used) is bool and type(shared) is bool and size is not None):
                    entry["policyEligibleBytes"] = size
            if not builder_known:
                entry["note"] += " Native builder binding is unknown; policy estimate is unavailable."
        elif kind == "volumes":
            usage = value.get("UsageData")
            usage = usage if isinstance(usage, dict) else {}
            size, refs = _integer(usage.get("Size")), _integer(usage.get("RefCount"))
            entry.update(logicalBytes=size, references=refs, policyEligibleBytes=0,
                         protected=_identifier(value.get("Name")) in {"amux-home", "amux-nix"})
            if refs is not None and size is not None:
                entry["nativeReclaimableBytes"] = size if refs == 0 else 0
            entry["note"] = "All volumes are report-only; amux-home and amux-nix are explicitly protected."
        else:
            entry.update(logicalBytes=_integer(value.get("SizeRw")), policyEligibleBytes=0)
            entry["note"] = "Containers and project/test artifacts are report-only, regardless of their names."
        if kind in ("images", "volumes"):
            entry["association"] = {"status": "unknown", "referencedBy": None,
                                    "observedReferencedBy": [], "reason": "Association evidence is unavailable"}
        items.append(entry)
    return {**empty, "count": len(items), "logicalBytes": _sum(items, "logicalBytes"),
            "nativeReclaimableBytes": _sum(items, "nativeReclaimableBytes"),
            "policyEligibleBytes": _sum(items, "policyEligibleBytes"), "items": items}


def inspect(*, budget: ScanBudget | None = None) -> dict:
    """Report engine metadata without changing engines, builders, objects or policy."""
    budget = budget or ScanBudget()
    result = {"status": "unknown", "reason": None, "target": None, "action": "report",
              "readOnly": True, "totalsAdditive": False, "projects": [], "warnings": []}
    for kind in ("images", "buildCache", "volumes", "containers"):
        result[kind] = _group(None, kind, 0, False, budget)
    try:
        target = _selection(budget)
        result["target"] = target
        target["daemonId"] = _daemon(target["endpoint"], budget)
        try:
            target["builder"] = _builder(target, budget)
        except (StorageError, OSError, ValueError, TypeError, KeyError) as exc:
            target["builder"] = None
            reason = str(exc) if isinstance(exc, StorageError) else "Docker builder metadata is invalid or unavailable"
            result["reason"] = reason
            result["warnings"].append("Native builder binding is unknown: " + reason + "; cache policy and maintenance are unavailable.")
        data = _get(target["endpoint"], "/system/df", budget)
        now = time.time()
        for kind, key in (("images", "Images"), ("buildCache", "BuildCache"),
                          ("volumes", "Volumes"), ("containers", "Containers")):
            result[kind] = _group(data.get(key), kind, now, target["builder"] is not None, budget)
        _associations(result, data, budget)
        # Engine-wide image storage avoids summing shared per-image layers.
        result["images"]["logicalBytes"] = _integer(data.get("LayersSize", data.get("ImagesSize")))
        projects = {}
        for kind in ("images", "buildCache", "volumes", "containers"):
            for entry in result[kind]["items"]:
                budget.check()
                project = entry["ownership"]["project"]
                if project:
                    projects.setdefault(project, []).append({"kind": kind, "id": entry["id"], "action": "report"})
        result["projects"] = [{"project": project, "objects": objects, "action": "report",
                               "test": None, "completionEvidence": None, "retentionEvidence": None}
                              for project, objects in projects.items()]
        groups_known = [result[kind]["count"] is not None
                        for kind in ("images", "buildCache", "volumes", "containers")]
        if not any(groups_known):
            result.update(status="unknown", reason="Docker disk-usage metadata is unavailable")
        else:
            result["status"] = "complete" if all(groups_known) and target["builder"] is not None else "partial"
    except (StorageError, OSError, ValueError, TypeError, KeyError) as exc:
        result["status"] = exc.status if isinstance(exc, _Unavailable) else "unknown"
        result["reason"] = str(exc) if isinstance(exc, _Unavailable) else "Docker inspection budget or metadata is unavailable"
    return result
