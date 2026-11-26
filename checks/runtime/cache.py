#!/usr/bin/env python3
"""Plan public offline-test dependencies; never build or activate a system."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

import snapshot

PUBLIC_CACHE = "https://cache.nixos.org"
STORE = re.compile(r"(?:/nix/store/)?[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+._?=-]+\Z")
SECRETS = ("home-passwd", "llm-credential-stepfun", "llm-credential-deepseek")
REAP_TIMEOUT = 1.0


class CacheError(ValueError):
    """A bounded operation could not establish the required evidence."""


class CommandCancelled(KeyboardInterrupt):
    """CLI cancellation must unwind the active owned child before exiting."""

    def __init__(self, signum: int):
        super().__init__("Nix command cancelled")
        self.signum = signum


@contextmanager
def cli_cancellation():
    def cancel(signum, frame):
        raise CommandCancelled(signum)

    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class CommandUnavailable(CacheError):
    def __init__(self, stdout: bytes, stderr: bytes):
        super().__init__("Nix command unavailable")
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class Budget:
    timeout: float = 30
    byte_cap: int = 4 * 1024 * 1024
    requests: int = 0
    nodes: int = 4096

    def __post_init__(self):
        if (not math.isfinite(self.timeout) or not 0 < self.timeout <= 600
                or not 1024 <= self.byte_cap <= 64 * 1024 * 1024
                or not 0 <= self.requests <= 10000 or not 1 <= self.nodes <= 100000):
            raise CacheError("Invalid resource budget")


def store_path(value: Any) -> str:
    if not isinstance(value, str) or STORE.fullmatch(value) is None:
        raise CacheError("Invalid store path")
    return value if value.startswith("/nix/store/") else "/nix/store/" + value


def info_row(value: Any, path: str) -> dict | None:
    """Use the legacy/list/versioned path-info shapes used by provenance.py."""
    if isinstance(value, list):
        rows = [row for row in value if isinstance(row, dict) and row.get("path") == path]
        return rows[0] if len(rows) == 1 else None
    if isinstance(value, dict):
        rows = value.get("info", value)
        if isinstance(rows, dict):
            row = rows.get(path, rows.get(Path(path).name))
            return row if isinstance(row, dict) else None
    return None


def metadata(value: Any, path: str, *, public: bool) -> dict:
    row = info_row(value, path)
    if row is None:
        raise CacheError("Missing path metadata")
    nar_hash = row.get("narHash")
    nar_size = row.get("narSize")
    refs = row.get("references")
    if (not isinstance(nar_hash, str) or not nar_hash.startswith("sha256-")
            or type(nar_size) is not int or nar_size < 0 or not isinstance(refs, list)):
        raise CacheError("Incomplete NAR metadata")
    result = {"path": path, "narHash": nar_hash, "narSize": nar_size,
              "references": sorted({store_path(ref) for ref in refs})}
    if public:
        # New Nix nests download metadata; old Nix places it on the row.
        download = row.get("download", {})
        size = row.get("fileSize", row.get("downloadSize"))
        if size is None and isinstance(download, dict):
            size = download.get("fileSize", download.get("size"))
        if type(size) is not int or size < 0:
            raise CacheError("Missing compressed size metadata")
        result["fileSize"] = size
    return result


class Nix:
    """Small fixed-argv Nix boundary with an empty user configuration."""

    def __init__(self, executable: str, work: Path, budget: Budget = Budget()):
        executable_path = Path(executable)
        if not executable_path.is_absolute() or not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise CacheError("An explicit executable Nix path is required")
        self.executable = str(executable_path)
        self.work = work
        self.budget = budget
        self.request_count = 0
        self.command_count = 0
        self.total_bytes = 0
        self._public_info: dict[str, dict] = {}
        home = work / "home"
        home.mkdir(mode=0o700)
        self.env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home),
                    "XDG_CACHE_HOME": str(home / "cache"), "PATH": os.defpath,
                    "NIX_CONF_DIR": str(home), "NIX_USER_CONF_FILES": os.devnull,
                    "NIX_PATH": "", "LC_ALL": "C", "TMPDIR": str(work)}

    def run(self, args: list[str], *, network: bool = False, json_output: bool = True,
            pass_fds: tuple[int, ...] = ()) -> Any:
        if network:
            if self.request_count >= self.budget.requests:
                raise CacheError("Public metadata request budget exhausted")
            self.request_count += 1
        options = ["--extra-experimental-features", "nix-command flakes",
                   "--option", "builders", "", "--option", "max-jobs", "0",
                   "--option", "allow-import-from-derivation", "false",
                   "--option", "accept-flake-config", "false",
                   "--option", "flake-registry", "", "--option", "substituters", "",
                   "--option", "extra-substituters", "", "--option", "netrc-file", "",
                   "--option", "connect-timeout", "5", "--option", "download-attempts", "1"]
        if not network:
            options.append("--offline")
        argv = [self.executable, *options, *args]
        self.command_count += 1
        output = {"out": bytearray(), "err": bytearray()}
        deadline = time.monotonic() + self.budget.timeout
        process = subprocess.Popen(argv, cwd=self.work, env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   pass_fds=pass_fds)
        try:
            with selectors.DefaultSelector() as selector:
                for stream, label in ((process.stdout, "out"), (process.stderr, "err")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, label)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CacheError("Nix command timed out")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        self.total_bytes += len(chunk)
                        if sum(map(len, output.values())) + len(chunk) > self.budget.byte_cap:
                            raise CacheError("Nix output byte cap exceeded")
                        output[key.data].extend(chunk)
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise CacheError("Nix command timed out") from exc
            if process.returncode:
                # Never print native diagnostics or interpret generic failures as 404.
                raise CommandUnavailable(bytes(output["out"]), bytes(output["err"]))
            if not json_output:
                return output["out"].decode("utf-8").strip()
            try:
                return json.loads(output["out"])
            except (ValueError, UnicodeError) as exc:
                raise CacheError("Nix returned invalid JSON") from exc
        finally:
            failed = sys.exc_info()[0] is not None
            cleanup_error = None
            try:
                # Only signal while the session leader is still ours to reap.
                # A completed wait has released its PID; never signal that ID.
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        cleanup_error = exc
                try:
                    process.wait(timeout=REAP_TIMEOUT)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    cleanup_error = exc
            finally:
                try:
                    process.stdout.close()
                finally:
                    process.stderr.close()
            if cleanup_error is not None and not failed:
                raise CacheError("Nix child cleanup unconfirmed") from cleanup_error

    def info(self, path: str, *, public: bool = False, destination: str | None = None,
             pass_fds: tuple[int, ...] = ()) -> dict:
        path = store_path(path)
        if public and path in self._public_info:
            row = self._public_info[path]
            return {**row, "references": list(row["references"])}
        args = ["path-info", "--json"]
        if public:
            args += ["--store", PUBLIC_CACHE]
        elif destination:
            args += ["--store", destination]
        try:
            value = self.run([*args, path], network=public, pass_fds=pass_fds)
        except CommandUnavailable as exc:
            # Nix may return null plus exit 1 for an absent path. A transport
            # error, even with the same null JSON, never proves a cache miss.
            diagnostic = exc.stderr.decode("utf-8", errors="replace").strip()
            if diagnostic != "error: path '" + path + "' is not valid":
                raise
            try:
                value = json.loads(exc.stdout)
            except (ValueError, UnicodeError):
                raise exc from None
        row = info_row(value, path)
        # Only an explicit null in a successful path-keyed response proves missing.
        rows = value.get("info", value) if isinstance(value, dict) else None
        if isinstance(rows, dict) and (path in rows or Path(path).name in rows) and row is None:
            key = path if path in rows else Path(path).name
            if rows[key] is None:
                return {"path": path, "status": "missing"}
        result = {**metadata(value, path, public=public), "status": "cached" if public else "nativeavailable"}
        if (public and self.request_count < self.budget.requests
                and any(ref != path and ref not in self._public_info for ref in result["references"])):
            try:
                value = self.run([*args, "--recursive", path], network=True, pass_fds=pass_fds)
                batch = {}
                pending = {path}
                while pending:
                    current = pending.pop()
                    if current in batch:
                        continue
                    if len(batch) >= self.budget.nodes:
                        raise CacheError("Public metadata node budget exhausted")
                    batch[current] = {**metadata(value, current, public=True), "status": "cached"}
                    pending.update(ref for ref in batch[current]["references"] if ref not in batch)
                if (batch[path] != result or len(self._public_info.keys() | batch.keys()) > self.budget.nodes
                        or any(self._public_info[key] != row for key, row in batch.items() if key in self._public_info)):
                    raise CacheError("Public metadata batch differs or exceeds node budget")
            except CacheError:
                # Only a complete, consistent reachable closure may grant memo hits.
                pass
            else:
                self._public_info.update(batch)
        return result

    def derivation(self, path: str) -> dict:
        value = self.run(["derivation", "show", store_path(path)])
        rows = value.get("derivations", value) if isinstance(value, dict) else None
        row = rows.get(path, rows.get(Path(path).name)) if isinstance(rows, dict) else None
        if not isinstance(row, dict):
            raise CacheError("Missing derivation metadata")
        return row


@contextmanager
def archive_transport(work: Path):
    """Bind both handles before writing bytes, then remove the only pathname."""
    directory = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = ".source-" + os.urandom(16).hex() + ".tar"
    writer = readonly = None
    try:
        writer = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        readonly = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        original = os.fstat(writer)
        observed = os.fstat(readonly)
        if (not stat.S_ISREG(observed.st_mode)
                or (original.st_dev, original.st_ino) != (observed.st_dev, observed.st_ino)):
            raise CacheError("Archive transport binding changed")
        os.unlink(name, dir_fd=directory)
        with os.fdopen(writer, "w+b") as stream:
            writer = None
            yield stream, readonly
    finally:
        if writer is not None:
            os.close(writer)
        if readonly is not None:
            os.close(readonly)
        os.close(directory)


def freeze_source(root: Path, nix: Nix) -> dict:
    """Transport only approved FD-read bytes through an unlinked archive FD."""
    with snapshot._SnapshotIO(root) as pinned:
        manifest = snapshot._verify_snapshot(pinned)
        if manifest["fixture"]["selectionStage"] != "minimal":
            raise CacheError("A reviewed minimal snapshot is required")
        lock = json.loads(pinned.read("flake.lock")[0])
        # The transport is unlinked before any native call. All archive members
        # are regular files/directories created from already verified FD bytes.
        with archive_transport(nix.work) as (transport, readonly):
            with tarfile.open(fileobj=transport, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for entry in manifest["files"]:
                    data, mode = pinned.read(entry["relativePath"])
                    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        raise CacheError("Snapshot changed during source freeze")
                    member = tarfile.TarInfo("source/" + entry["relativePath"])
                    member.size = len(data)
                    member.mode = mode
                    archive.addfile(member, io.BytesIO(data))
                data = pinned.read(snapshot.MANIFEST_NAME)[0]
                member = tarfile.TarInfo("source/" + snapshot.MANIFEST_NAME)
                member.size = len(data)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(data))
            transport.flush()
            transport.seek(0)
            # The archive name was removed before any source bytes were written.
            # Native Nix receives only the independently opened readonly handle.
            pinned.check()
            value = nix.run(["store", "prefetch-file", "--unpack", "--json", "--name", "source",
                             "file:///dev/fd/" + str(readonly)], pass_fds=(readonly,))
        if not isinstance(value, dict):
            raise CacheError("Missing source transport result")
        path = store_path(value.get("storePath"))
        nar_hash = value.get("hash")
        # Nix normalizes modes. Reuse the full fixed-FD verifier, adapting only
        # its observed mode after first requiring the expected canonical mode.
        expected_modes = {entry["relativePath"]: int(entry["mode"], 8) for entry in manifest["files"]}
        with snapshot._SnapshotIO(Path(path)) as frozen:
            class NormalizedView:
                fd = frozen.fd

                def directory(self, name):
                    return frozen.directory(name)

                def read(self, name):
                    data, mode = frozen.read(name)
                    if name in expected_modes:
                        expected = expected_modes[name]
                        if mode != (0o555 if expected & 0o111 else 0o444):
                            raise CacheError("Unexpected normalized store mode")
                        mode = expected
                    return data, mode

                def check(self):
                    frozen.check()
            if snapshot._verify_snapshot(NormalizedView()) != manifest:
                raise CacheError("Frozen source differs from verified snapshot")
        pinned.check()
    native = nix.info(path)
    if native["narHash"] != nar_hash:
        raise CacheError("Frozen store metadata differs from transport NAR")
    return {"path": path, "narHash": native["narHash"], "lockSha256": manifest["lockSha256"],
            "manifest": manifest, "lock": lock, "metadata": native}


def root_expression(source: dict) -> str:
    # This is a preparation-only extension of the real Home Manager composition.
    # It is never written into the snapshot or delivered to the guest.
    ref = "path:" + source["path"] + "?narHash=" + source["narHash"]
    overrides = " ".join(f'sops.secrets.{name}.sopsFile = lib.mkForce placeholder;' for name in SECRETS)
    return '''let
      f = builtins.getFlake REF;
      original = f.homeConfigurations.runtime-min;
      extended = original.extendModules { modules = [ ({ lib, ... }: let
        placeholder = builtins.toFile "envy-runtime-public-placeholder.yaml"
          "home:\\n  passwd: PUBLIC-TEST-ONLY\\nllm:\\n  steps:\\n    apikey: PUBLIC-TEST-ONLY\\n  deepseek:\\n    apikey: PUBLIC-TEST-ONLY\\n";
        in { OVERRIDES sops.validateSopsFiles = lib.mkForce false; }) ]; };
    in { activation = extended.config.home.activationPackage.drvPath;
         setup = f.packages.x86_64-linux.setup.drvPath;
         extensionAvailable = original ? extendModules;
         secretNames = builtins.attrNames original.config.sops.secrets;
    }'''.replace("REF", json.dumps(ref)).replace("OVERRIDES", overrides)


def source_expression(source: dict) -> str:
    """Project only source arguments from the frozen production package owner."""
    ref = "path:" + source["path"] + "?narHash=" + source["narHash"]
    return '''let
      f = builtins.getFlake REF;
      pkgs = import f.inputs.nixpkgs { system = "x86_64-linux"; config = {}; overlays = []; };
      package = pkgs.callPackage (f.outPath + "/modules/cores/packages/envy-tui.nix") {
        rustPlatform = pkgs.rustPlatform // { buildRustPackage = args: {
          production = pkgs.rustPlatform.buildRustPackage args;
          inherit (args) src cargoLock;
        }; };
      };
    in {
      binding = { path = f.outPath; narHash = HASH;
        lockSha256 = builtins.hashFile "sha256" (f.outPath + "/flake.lock"); };
      input = { path = f.inputs.nixpkgs.outPath; narHash = f.inputs.nixpkgs.narHash; };
      sources = {
        tui = builtins.path { path = package.src; };
        cargoLock = builtins.path { path = package.cargoLock.lockFile; };
        sourceStdenv = builtins.path { path = f.inputs.nixpkgs.outPath + "/pkgs/stdenv/generic/source-stdenv.sh"; };
        defaultBuilder = builtins.path { path = f.inputs.nixpkgs.outPath + "/pkgs/stdenv/generic/default-builder.sh"; };
      };
    }'''.replace("REF", json.dumps(ref)).replace("HASH", json.dumps(source["narHash"]))


def derivation_inputs(row: dict) -> tuple[dict[str, list[str]], list[str]]:
    """Normalize legacy inputDrvs/inputSrcs and Nix 2.33 versioned inputs."""
    drvs = row.get("inputDrvs")
    sources = row.get("inputSrcs")
    if drvs is None or sources is None:
        inputs = row.get("inputs")
        if not isinstance(inputs, dict):
            raise CacheError("Unknown derivation input shape")
        drvs = inputs.get("drvs")
        sources = inputs.get("srcs")
    if not isinstance(drvs, dict) or not isinstance(sources, list):
        raise CacheError("Unknown derivation input shape")
    result = {}
    for path, outputs in drvs.items():
        if isinstance(outputs, dict):
            if outputs.get("dynamicOutputs"):
                raise CacheError("Dynamic derivation inputs need explicit support")
            outputs = outputs.get("outputs")
        if not isinstance(outputs, list) or any(not isinstance(name, str) for name in outputs):
            raise CacheError("Unknown required outputs")
        result[store_path(path)] = outputs
    return result, [store_path(path) for path in sources]


def output_path(row: dict, name: str) -> str:
    outputs = row.get("outputs")
    value = outputs.get(name) if isinstance(outputs, dict) else None
    if (isinstance(value, dict) and "path" not in value and row.get("version") == 4
            and isinstance(value.get("hash"), str) and value["hash"].startswith("sha256-")
            and value.get("method") in {"flat", "nar"}):
        # Nix schema v4 omits the path of a statically hashed output. The
        # derivation still binds its concrete output in env.<output-name>.
        env = row.get("env")
        return store_path(env.get(name) if isinstance(env, dict) else None)
    return store_path(value.get("path") if isinstance(value, dict) else value)


class Planner:
    def __init__(self, nix: Nix, source: dict):
        self.nix = nix
        self.source = source
        self.records: dict[str, dict] = {}
        self.derivations: dict[str, dict] = {}
        self.visited: set[tuple[str, str]] = set()
        self.gaps: list[dict] = []
        self.allowed_sources = {source["path"]}
        self.checked_sources: set[str] = set()
        self.graph_context: dict[str, str] = {"step": "root-eval"}
        self.inputs_complete = False
        self.source_proofs: dict[str, dict] | None = None

    def gap(self, kind: str, path: str | None = None, **details):
        gap = {"kind": kind, **details}
        if path is not None:
            gap["path"] = path
        if gap not in self.gaps:
            self.gaps.append(gap)

    def limit(self):
        if len(self.records) + len(self.visited) >= self.nix.budget.nodes:
            raise CacheError("Graph node budget exhausted")

    def cached(self, path: str) -> str:
        if path in self.records:
            return self.records[path]["status"]
        self.limit()
        try:
            row = self.nix.info(path, public=True)
        except CacheError:
            self.records[path] = {"path": path, "status": "unavailable"}
            self.gap("metadata-unavailable", path)
            return "unavailable"
        self.records[path] = row
        if row["status"] == "cached":
            for ref in row["references"]:
                if self.cached(ref) != "cached":
                    self.gap("incomplete-public-closure", ref)
        return row["status"]

    def source_binding(self) -> dict:
        return {key: self.source[key] for key in ("path", "narHash", "lockSha256")}

    def prove_sources(self):
        # Four owner-selected values only. No candidate path/body is passed to
        # Nix; a foreign basename cannot influence expected-path reconstruction.
        self.source_proofs = {}
        if not self.inputs_complete:
            return
        try:
            lock = self.source["lock"]
            nodes = lock["nodes"]
            node = nodes[nodes[lock["root"]]["inputs"]["nixpkgs"]]
            locked_hash = node["locked"]["narHash"]
            binding = self.source_binding()
            value = self.nix.run(["eval", "--json", "--expr", source_expression(self.source)])
            owner = value["input"]
            owner_path = store_path(owner["path"])
            owner_row = self.records.get(owner_path, {})
            if (value["binding"] != binding or self.source_binding() != binding
                    or owner["narHash"] != locked_hash or owner_path not in self.allowed_sources
                    or owner_row.get("narHash") != locked_hash
                    or owner_row.get("status") != "nativeavailable"):
                raise CacheError("Source proof binding differs from current snapshot or lock")
            owners = {
                "tui": {"path": self.source["path"], "relativePath": "modules/cores/packages/envy-tui.nix"},
                "cargoLock": {"path": self.source["path"], "relativePath": "modules/cores/packages/envy-tui.nix"},
                "sourceStdenv": {"path": owner_path, "relativePath": "pkgs/stdenv/generic/make-derivation.nix"},
                "defaultBuilder": {"path": owner_path, "relativePath": "pkgs/stdenv/generic/make-derivation.nix"},
            }
            if set(value["sources"]) != set(owners):
                raise CacheError("Unexpected source proof shape")
            proofs = {}
            for name, candidate in value["sources"].items():
                expected = store_path(candidate)
                native = self.nix.info(expected)
                if native["path"] != expected or native["status"] != "nativeavailable":
                    raise CacheError("Expected source unavailable")
                proofs[expected] = {"binding": binding, "lockedInput": dict(owner),
                                    "owner": owners[name], "metadata": native}
            self.source_proofs = proofs
        except (CacheError, KeyError, TypeError, ValueError):
            # A failed proof never authorizes even one candidate. The existing
            # unknown-source guard below reports each unresolved inputSrc.
            return

    def local_source(self, path: str):
        if path in self.checked_sources:
            return
        self.limit()
        self.checked_sources.add(path)
        # Host existence alone never authorizes exporting an inputSrcs object.
        if path not in self.allowed_sources and self.source_proofs is None:
            self.prove_sources()
        proof = (self.source_proofs or {}).get(path)
        if path not in self.allowed_sources and (proof is None or proof["binding"] != self.source_binding()):
            self.gap("unverified-source", path)
            self.records.setdefault(path, {"path": path, "status": "unknown", "kind": "source"})
            return
        try:
            row = self.records.get(path)
            if row is None or row["status"] != "nativeavailable":
                row = self.nix.info(path)
            if proof is not None and any(row.get(key) != proof["metadata"].get(key)
                                         for key in ("path", "narHash", "narSize", "references", "status")):
                raise CacheError("Native source differs from expected NAR")
            self.records[path] = {**row, "kind": "source", "provenance": "verified-snapshot-or-locked-input"}
            if proof is not None:
                self.records[path]["origin"] = {key: proof[key] for key in ("binding", "lockedInput", "owner")}
                self.records[path]["origin"].update(expectedPath=path, narHash=row["narHash"])
            for ref in row["references"]:
                self.local_source(ref)
        except CacheError:
            self.gap("source-unavailable", path)

    def visit(self, drv: str, names: list[str]):
        todo = [name for name in names if (drv, name) not in self.visited]
        if not todo:
            return
        self.graph_context = {"step": "derivation-show", "drv": drv}
        self.limit()
        self.visited.update((drv, name) for name in todo)
        row = self.nix.derivation(drv)
        self.derivations[drv] = row
        uncached = []
        for name in todo:
            self.graph_context = {"step": "output-path", "drv": drv}
            path = output_path(row, name)
            state = self.cached(path)
            if state == "missing":
                uncached.append(path)
            elif state != "cached":
                self.gap("unknown-output", path)
        if not uncached:
            # A substitutable output needs its runtime refs, never its build inputs.
            return
        self.graph_context = {"step": "derivation-inputs", "drv": drv}
        inputs, sources = derivation_inputs(row)
        system = row.get("system")
        fixed = any(isinstance(out, dict) and (out.get("hash") or out.get("method"))
                    for out in row.get("outputs", {}).values())
        for path in uncached:
            self.records[path].update(kind="FOD" if fixed else "local", drv=drv,
                                      status="missing" if fixed else "needsguestbuild", system=system)
            if fixed:
                self.gap("FOD-cache-missing", path)
            elif system != "x86_64-linux":
                self.gap("unsupported-build-system", path)
        # The guest evaluates these derivations from verified sources itself.
        # Exporting a .drv would also require its full build-input .drv closure,
        # crossing the deliberate stop at cached dependency outputs.
        for path in sources:
            self.local_source(path)
        for path, required in inputs.items():
            self.visit(path, required)

    def locked_inputs(self):
        try:
            archive = self.nix.run(["flake", "archive", "--dry-run", "--json", "--no-update-lock-file",
                                    "--no-write-lock-file", "path:" + self.source["path"] + "?narHash=" + self.source["narHash"]])
            archive_root = store_path(archive["path"])
            if archive_root != self.source["path"]:
                root_info = self.nix.info(archive_root)
                if root_info["narHash"] != self.source["narHash"]:
                    raise CacheError("Archive root differs from frozen snapshot")
                self.allowed_sources.add(archive_root)
                self.records[archive_root] = {**root_info, "kind": "source", "provenance": "verified-snapshot"}
            nodes = self.source["lock"]["nodes"]
            expected = {node["locked"]["narHash"] for node in nodes.values() if "locked" in node}
            seen = set()
            def walk(value):
                for child in value.get("inputs", {}).values():
                    self.limit()
                    path = store_path(child["path"])
                    info = self.nix.info(path)
                    if info["narHash"] not in expected:
                        raise CacheError("Archive input is not a locked source")
                    seen.add(info["narHash"])
                    self.allowed_sources.add(path)
                    self.records[path] = {**info, "kind": "source", "provenance": "locked-input"}
                    walk(child)
            walk(archive)
            if seen != expected:
                raise CacheError("Incomplete locked input archive")
            self.inputs_complete = True
            for path in sorted(self.allowed_sources):
                self.local_source(path)
            return {"status": "complete", "count": len(seen)}
        except (CacheError, KeyError, TypeError, ValueError, RecursionError):
            self.gap("locked-inputs-unavailable")
            return {"status": "unavailable"}

    def plan(self) -> dict:
        inputs = self.locked_inputs()
        self.local_source(self.source["path"])
        roots = None
        try:
            self.graph_context = {"step": "root-eval"}
            roots = self.nix.run(["eval", "--json", "--expr", root_expression(self.source)])
            self.graph_context = {"step": "root-contract"}
            if roots.get("extensionAvailable") is not True or set(roots.get("secretNames", [])) != set(SECRETS):
                raise CacheError("Unexpected Home Manager extension contract")
            for name in ("activation", "setup"):
                self.visit(store_path(roots[name]), ["out"])
        except (CacheError, KeyError, TypeError, RecursionError) as exc:
            reason = {CommandUnavailable: "command-unavailable", KeyError: "missing-field",
                      TypeError: "invalid-type", RecursionError: "recursion-limit"}.get(type(exc), "evidence-unavailable")
            self.gap("root-or-graph-unavailable", reason=reason, **self.graph_context)
        records = sorted(self.records.values(), key=lambda row: row["path"])
        public = [row for row in records if row["status"] == "cached"]
        return {"schemaVersion": 1, "status": "blocked" if self.gaps else "planned",
                "purpose": "test-preparation-only", "source": {key: self.source[key] for key in ("path", "narHash", "lockSha256")},
                "roots": roots, "overlay": {"scope": "prefetch-only", "sopsFile": list(SECRETS),
                    "guestDerivationEquivalent": False, "validateSopsFiles": False},
                "lockedInputs": inputs, "objects": records, "gaps": self.gaps,
                "publicCache": PUBLIC_CACHE, "requestCount": self.nix.request_count,
                "limits": {"timeoutSecondsPerCommand": self.nix.budget.timeout,
                           "outputBytesPerCommand": self.nix.budget.byte_cap,
                           "remoteOperations": self.nix.budget.requests, "graphNodes": self.nix.budget.nodes},
                "requestCountScope": "Nix CLI remote operations; HTTP retries/internal requests are not observable",
                "totals": {"publicObjectCount": len(public),
                    "fileSize": sum(row["fileSize"] for row in public) if not self.gaps else None,
                    "narSize": sum(row["narSize"] for row in public) if not self.gaps else None,
                    "knownFileSize": sum(row["fileSize"] for row in public),
                    "knownNarSize": sum(row["narSize"] for row in public),
                    "complete": not self.gaps},
                "ready": False}


def preflight(root: Path, nix: Nix) -> dict:
    return Planner(nix, freeze_source(root, nix)).plan()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight",))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--nix", required=True)
    parser.add_argument("--metadata-requests", type=int, default=0,
                        help="Explicit public metadata operation budget; 0 keeps preflight offline")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--byte-cap", type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()
    try:
        budget = Budget(timeout=args.timeout, byte_cap=args.byte_cap, requests=args.metadata_requests)
        with cli_cancellation(), tempfile.TemporaryDirectory(prefix="envy-cache-") as directory:
            nix = Nix(args.nix, Path(directory), budget)
            result = preflight(args.snapshot, nix)
        print(json.dumps(result, sort_keys=True))
    except CommandCancelled as exc:
        raise SystemExit(128 + exc.signum) from None
    except (CacheError, snapshot.SnapshotError, OSError, ValueError) as exc:
        print(json.dumps({"schemaVersion": 1, "status": "unavailable", "ready": False,
                          "error": type(exc).__name__}, sort_keys=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
