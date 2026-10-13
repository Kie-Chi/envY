"""Opt-in software source E2E tests: real CLI/Nix, or a disposable Linux container.

Run `... e2e_software_sources.py cli` in the Nix devShell. Run `native` only in
a fresh container: it creates repositories and installs/removes a fixture package.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
REPORT = {"suite": None, "checks": []}


def run(args, *, cwd=None, env=None, check=True, timeout=240):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise AssertionError(f"{Path(str(args[0])).name} failed ({result.returncode}):\n{result.stderr[-3000:]}\n{result.stdout[-2000:]}")
    return result


class CliE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="envy-source-e2e-")
        cls.base = Path(cls.temporary.name)
        cls.root = cls.base / "checkout"
        cls.root.mkdir()
        files = run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=REPOSITORY).stdout.split("\0")
        for relative in filter(None, files):
            original = REPOSITORY / relative
            if not original.exists():
                continue
            target = cls.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target, follow_symlinks=False)
        run(["git", "init", "-q"], cwd=cls.root)
        run(["git", "add", "."], cwd=cls.root)
        run(["git", "-c", "user.name=envY E2E", "-c", "user.email=e2e@example.invalid", "commit", "-qm", "fixture"], cwd=cls.root)
        platform = "darwin" if sys.platform == "darwin" else "linux"
        cls.machine = sorted((cls.root / "hosts" / platform).glob("*.nix"))[0]
        cls.original_machine = cls.machine.read_bytes()
        cls.env = {**os.environ, "ENVY_ROOT": str(cls.root), "ENVY_MACHINE": cls.machine.stem,
                   "PYTHONPATH": str(cls.root / "resources/scripts"), "ENVY_ACTIVITY": "0",
                   "XDG_CACHE_HOME": str(cls.base / "cache"), "XDG_STATE_HOME": str(cls.base / "state")}
        # Share Nix's normal fetcher cache/store, while keeping envY caches private.
        # This wrapper only sets the cache directory and executes the real binary.
        real_nix = shutil.which("nix")
        if not real_nix:
            raise RuntimeError("CLI E2E requires the Nix devShell")
        original_cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        bin_dir = cls.base / "bin"
        bin_dir.mkdir()
        nix = bin_dir / "nix"
        nix.write_text(f'#!{sys.executable}\nimport os, sys\nenv = os.environ.copy()\nenv["XDG_CACHE_HOME"] = {original_cache!r}\nos.execve({real_nix!r}, [{real_nix!r}, *sys.argv[1:]], env)\n')
        nix.chmod(0o755)
        cls.env["PATH"] = str(bin_dir) + os.pathsep + cls.env["PATH"]
        cls.lock = (cls.root / "flake.lock").read_bytes()
        cls.flake = (cls.root / "flake.nix").read_bytes()
        cls.web = cls.base / "web"
        cls.web.mkdir()
        for name, platforms in (("tools", ["aarch64-darwin", "x86_64-linux"]), ("linux-only", ["x86_64-linux"]), ("empty", [])):
            fixture = cls.base / ("flake-" + name)
            fixture.mkdir()
            outputs = " ".join(f'"{system}".default = builtins.derivation {{ system = "{system}"; name = "envy-e2e-locked-1.0"; pname = "envy-e2e-locked"; version = "1.0"; builder = "/bin/sh"; }};' for system in platforms)
            (fixture / "flake.nix").write_text('{ outputs = { self, ... }: { packages = { ' + outputs + ' }; }; }\n')
            with tarfile.open(cls.web / (name + ".tar.gz"), "w:gz") as archive:
                archive.add(fixture, arcname="source")
        metadata = cls.web / "pypi/envy-e2e-python/json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text('{"info":{"name":"envy-e2e-python","version":"1.0"}}')
        cert, key = cls.base / "cli-cert.pem", cls.base / "cli-key.pem"
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
             "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"])
        class Handler(http.server.SimpleHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(cls.web), **kwargs)
            def log_message(self, *args):
                pass
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"https://localhost:{cls.server.server_port}"
        cls.env.update(SSL_CERT_FILE=str(cert), NIX_SSL_CERT_FILE=str(cert))
        cls.env["NIX_CONFIG"] = cls.env.get("NIX_CONFIG", "") + f"\nssl-cert-file = {cert}\n"
        global_source = cls.base / "global-nixpkgs"
        global_source.mkdir()
        outputs = " ".join(f'"{system}".hello = builtins.derivation {{ system = "{system}"; name = "hello-999"; pname = "hello"; version = "999"; builder = "/bin/sh"; }};' for system in ["aarch64-darwin", "x86_64-linux"])
        (global_source / "flake.nix").write_text('{ outputs = { self }: { packages = { ' + outputs + ' }; }; }\n')
        registry = cls.base / "registry.json"
        registry.write_text(json.dumps({"version": 2, "flakes": [{"from": {"type": "indirect", "id": "nixpkgs"},
            "to": {"type": "path", "path": str(global_source)}}]}))
        cls.env["NIX_CONFIG"] += f"flake-registry = {registry}\n"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def cli(self, *arguments, ok=True):
        result = run([sys.executable, "-B", "-m", "envy", *arguments, "--json"], cwd=self.root, env=self.env, check=False)
        payload = json.loads(result.stdout)  # Also verifies exactly one JSON document.
        self.assertEqual(result.returncode == 0, ok, result.stderr + "\n" + result.stdout)
        if "ok" in payload:
            self.assertEqual(payload["ok"], ok)
        return payload

    def tearDown(self):
        self.machine.write_bytes(self.original_machine)
        (self.root / "flake.nix").write_bytes(self.flake)
        (self.root / "flake.lock").write_bytes(self.lock)

    def test_catalog_locked_search_add_offline_remove(self):
        catalog = self.cli("sw", "source", "ls")
        source = next(s for s in catalog["data"]["sources"] if s["id"] == "nix:nixpkgs")
        self.assertTrue(source["registered"] and source["ready"])
        self.assertTrue(source["context"]["locked"])
        global_version = run(["nix", "eval", "--raw", "nixpkgs#hello.version"], cwd=self.root, env=self.env).stdout
        self.assertEqual(global_version, "999")
        search = self.cli("sw", "se", "hello", "--source-id", "nix:nixpkgs", "--refresh", "--limit", "100")
        self.assertTrue(any(row["ref"] == "nix:hello" for row in search["results"]))
        self.assertTrue(all(row["version"] != "999" for row in search["results"] if row["ref"] == "nix:hello"))
        self.assertTrue(all(row["sourceId"] == "nix:nixpkgs" for row in search["results"]))
        preview = self.cli("sw", "add", "nix.user.package", "nix:sl", "--dry-run")
        self.assertTrue(preview["data"]["plan"]["changed"])
        self.assertEqual(self.machine.read_bytes(), self.original_machine)
        offline = self.cli("sw", "add", "nix.user.package", "nix:sl", "--dry-run", "--offline")
        self.assertEqual(preview["data"]["plan"], offline["data"]["plan"])
        self.cli("sw", "add", "nix.user.package", "nix:sl", "--yes", "--offline")
        selections = self.cli("sw", "ls", "--details")["groups"]
        nix_group = next(group for group in selections if group["id"] == "nix.user.package")
        row = next(item for item in nix_group["items"] if item["id"] == "sl")
        self.assertTrue(row["effective"])
        self.assertEqual(row["sourceId"], "nix:nixpkgs")
        self.assertIsNone(row["version"])
        self.cli("sw", "rm", "nix.user.package", "sl", "--yes", "--clean")
        self.assertFalse('pkgs."sl"' in self.machine.read_text(), "managed include remained after clean removal")
        self.assertEqual((self.root / "flake.lock").read_bytes(), self.lock)

    def test_shared_registration_preview_success_and_remove(self):
        ref = self.endpoint + "/tools.tar.gz"
        preview = self.cli("sw", "source", "add", "nix:e2e-tools", "--ref", ref, "--dry-run")
        self.assertIn('"e2e-tools".url', preview["data"]["plan"]["diff"])
        self.assertEqual((self.root / "flake.nix").read_bytes(), self.flake)
        self.assertEqual((self.root / "flake.lock").read_bytes(), self.lock)
        self.cli("sw", "source", "add", "nix:e2e-tools", "--ref", ref, "--yes")
        source = self.cli("sw", "source", "show", "nix:e2e-tools")["data"]["source"]
        self.assertTrue(source["registered"])
        self.assertTrue(source["context"]["locked"]["narHash"])
        self.cli("sw", "add", "nix.user.package", "flake:e2e-tools#default", "--yes")
        denied = self.cli("sw", "source", "rm", "nix:e2e-tools", "--yes", ok=False)
        self.assertEqual(denied["error"]["code"], "source-in-use")
        self.cli("sw", "rm", "nix.user.package", "envy-e2e-locked", "--yes", "--clean")
        self.cli("sw", "source", "rm", "nix:e2e-tools", "--yes")
        self.assertFalse('"e2e-tools".url' in (self.root / "flake.nix").read_text(), "shared input remained after removal")

    def test_single_platform_source_exposes_supported_platform(self):
        self.cli("sw", "source", "add", "nix:e2e-linux", "--ref", self.endpoint + "/linux-only.tar.gz", "--yes")
        source = self.cli("sw", "source", "show", "nix:e2e-linux")["data"]["source"]
        self.assertEqual(source["platforms"], ["linux"])
        self.assertEqual(source["ready"], sys.platform != "darwin")

    def test_shared_validation_failure_restores_files(self):
        result = self.cli("sw", "source", "add", "nix:e2e-no-packages", "--ref", self.endpoint + "/empty.tar.gz", "--yes", ok=False)
        self.assertEqual(result["error"]["code"], "shared-validation-failed")
        self.assertIn("no packages", result["error"]["message"])
        self.assertEqual((self.root / "flake.nix").read_bytes(), self.flake)
        self.assertEqual((self.root / "flake.lock").read_bytes(), self.lock)

    def test_machine_source_add_dependency_rejection_and_remove(self):
        endpoint = self.endpoint
        definition = self.base / "public-registry.json"
        definition.write_text(json.dumps({"label": "E2E registry", "ecosystem": "pypi", "type": "registry",
                                           "parameters": {"index": endpoint + "/simple", "metadata": endpoint + "/pypi"}}))
        preview = self.cli("sw", "source", "add", "pypi:e2e", "--definition", str(definition), "--dry-run")
        self.assertIn("ENVY MANAGED SOURCES", preview["data"]["plan"]["diff"])
        self.assertEqual(self.machine.read_bytes(), self.original_machine)
        self.cli("sw", "source", "add", "pypi:e2e", "--definition", str(definition), "--yes")
        self.cli("sw", "add", "pypi.user.tool", "envy-e2e-python", "--source-id", "pypi:e2e", "--yes")
        denied = self.cli("sw", "source", "rm", "pypi:e2e", "--yes", ok=False)
        self.assertEqual(denied["error"]["code"], "source-in-use")
        self.cli("sw", "rm", "pypi.user.tool", "envy-e2e-python", "--yes", "--clean")
        self.cli("sw", "source", "rm", "pypi:e2e", "--yes")
        self.assertFalse("ENVY MANAGED SOURCES" in self.machine.read_text(), "source definition remained after removal")


class NativeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path("/.dockerenv").exists() or os.geteuid() != 0:
            raise RuntimeError("Native E2E requires a fresh root Docker container")
        cls.temporary = tempfile.TemporaryDirectory(prefix="envy-native-e2e-")
        cls.root = Path(cls.temporary.name)
        cls.root.chmod(0o755)
        cls.gpg = cls.root / "gpg"
        cls.gpg.mkdir(mode=0o700)
        run(["gpg", "--batch", "--homedir", str(cls.gpg), "--passphrase", "", "--quick-generate-key", "envY E2E <e2e@example.invalid>", "rsa2048", "sign", "0"])
        keys = run(["gpg", "--batch", "--homedir", str(cls.gpg), "--with-colons", "--list-keys"]).stdout
        cls.fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
        cls.web = cls.root / "web"
        cls.web.mkdir()
        (cls.web / "key.asc").write_text(run(["gpg", "--batch", "--homedir", str(cls.gpg), "--armor", "--export", cls.fingerprint]).stdout)
        cls.arch = run(["dpkg", "--print-architecture"]).stdout.strip()
        cls.debs = {}
        for directory, version in (("base", "9.0"), ("vendor", "1.0")):
            repo = cls.web / directory
            repo.mkdir()
            package = cls.root / ("package-" + directory)
            (package / "DEBIAN").mkdir(parents=True)
            (package / "DEBIAN/control").write_text(f"Package: envy-e2e-fixture\nVersion: {version}\nArchitecture: {cls.arch}\nMaintainer: envY E2E <e2e@example.invalid>\nDescription: disposable source constraint fixture\nDepends: dash\n")
            (package / "usr/share/envy-e2e").mkdir(parents=True)
            (package / "usr/share/envy-e2e/version").write_text(version)
            deb = repo / "fixture.deb"
            run(["dpkg-deb", "--build", str(package), str(deb)])
            (repo / "Packages").write_text(run(["dpkg-scanpackages", ".", "/dev/null"], cwd=repo).stdout)
            (repo / "Release").write_text(run(["apt-ftparchive", "release", "."], cwd=repo).stdout)
            run(["gpg", "--batch", "--homedir", str(cls.gpg), "--passphrase", "", "--clearsign", "--output", str(repo / "InRelease"), str(repo / "Release")])
            cls.debs[directory] = deb
        cert, key = cls.root / "cert.pem", cls.root / "tls.key"
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
             "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"])
        shutil.copy2(cert, "/usr/local/share/ca-certificates/envy-e2e.crt")
        run(["update-ca-certificates"])
        class Handler(http.server.SimpleHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(cls.web), **kwargs)
            def log_message(self, *args):
                pass
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://localhost:{cls.server.server_port}"
        from envy.sources.model import Source
        cls.source = Source.from_dict("native:e2e-vendor", {"label": "E2E vendor", "ecosystem": "native", "type": "native", "platforms": ["linux"],
            "parameters": {"apt": {"uri": cls.url + "/vendor", "suites": ["./"], "keyUrl": cls.url + "/key.asc", "keyFingerprint": cls.fingerprint}}})
        cls.sources = Path("/etc/apt/sources.list.d")
        # All changes are confined to this disposable container.
        for path in cls.sources.glob("*"):
            path.unlink()
        from envy.sources.native import os_release
        cls.distro_uri = "http://deb.debian.org/debian" if os_release().get("ID") == "debian" else "http://archive.ubuntu.com/ubuntu"
        Path("/etc/apt/sources.list").write_text(f"deb {cls.distro_uri} stable main\n")
        keyring = Path("/etc/apt/keyrings/e2e-base.gpg")
        keyring.parent.mkdir(exist_ok=True)
        keyring.write_bytes(subprocess.check_output(["gpg", "--batch", "--homedir", str(cls.gpg), "--export", cls.fingerprint]))
        (cls.sources / "envy-mirror.sources").write_text(f"Types: deb\nURIs: {cls.url}/base\nSuites: ./\nSigned-By: {keyring}\n")
        cls.external = cls.sources / "external-vendor.list"
        cls.external.write_text(f"deb [signed-by={keyring}] {cls.url}/vendor ./\n")
        cls.external_text = cls.external.read_bytes()
        cls.main_text = Path("/etc/apt/sources.list").read_bytes()

    @classmethod
    def tearDownClass(cls):
        run(["dpkg", "--remove", "envy-e2e-fixture"], check=False)
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def test_signed_source_candidate_install_and_artifact(self):
        from envy.sources.native.apt import effective_files, effective_options, signed_options
        from envy.sources.native.backend import activate, source_filename
        from envy.sources.adapters import resolve_native_source
        from envy.sources.artifacts import download, inspect
        from envy.sources.model import SourceError
        effective = "\n".join(effective_files().values())
        self.assertNotIn(self.distro_uri, effective)
        self.assertIn(self.url + "/base", effective)
        self.assertIn(self.url + "/vendor", effective)
        activate({self.source.id: self.source.to_dict()}, [self.source.id], "apt")
        self.assertFalse((self.sources / (source_filename(self.source) + ".sources")).exists())
        self.assertEqual(self.external.read_bytes(), self.external_text)
        self.assertEqual(Path("/etc/apt/sources.list").read_bytes(), self.main_text)
        with effective_options() as options:
            run(["apt-get", *options, "update"])
            policy = run(["apt-cache", *options, "policy", "envy-e2e-fixture"]).stdout
            self.assertIn("Candidate: 9.0", policy)
        resolved = resolve_native_source(self.source, "envy-e2e-fixture", 30)
        self.assertEqual(resolved.status, "found", resolved.error)
        self.assertEqual(resolved.result.version, "1.0")
        self.assertEqual(resolved.result.source_id, self.source.id)
        missing = resolve_native_source(self.source, "envy-e2e-absent", 30)
        self.assertEqual(missing.status, "not_found")
        definitions = self.root / "native-packages.json"
        definitions.write_text(json.dumps({"sources": {self.source.id: self.source.to_dict()}, "packages": [
            {"name": "envy-e2e-fixture", "sourceId": self.source.id}]}))
        # Match Nix activation's plain Python runtime: no site-packages.
        run([sys.executable, "-S", "-B", "-m", "envy.sources.native.backend", "install", "--definitions", str(definitions)])
        self.assertEqual(run(["dpkg-query", "-W", "-f=${Version}", "envy-e2e-fixture"]).stdout, "1.0")
        params = {"url": self.url + "/vendor/fixture.deb", "format": "deb", "packageName": "envy-e2e-fixture",
                  "version": "1.0", "architecture": self.arch, "hash": hashlib.sha256(self.debs["vendor"].read_bytes()).hexdigest()}
        target = self.root / "artifact.deb"
        descriptor = self.root / "artifact.json"
        descriptor.write_text(json.dumps(params))
        run([sys.executable, "-S", "-B", "-m", "envy.sources.artifacts", str(descriptor), str(target)])
        self.assertEqual(inspect(params, target), ("envy-e2e-fixture", "1.0", self.arch))
        run(["dpkg", "--remove", "envy-e2e-fixture"])
        with effective_options() as options:
            run(["apt-get", *options, "install", "-y", str(target)])
        self.assertEqual(run(["dpkg-query", "-W", "-f=${Version}", "envy-e2e-fixture"]).stdout, "1.0")
        with self.assertRaises(SourceError):
            download({**params, "hash": "0" * 64}, target)
        with self.assertRaises(SourceError):
            inspect({**params, "packageName": "wrong"}, self.debs["vendor"])
        wrong = self.source.to_dict()
        wrong["parameters"]["apt"]["keyFingerprint"] = "A" * 40
        self.external.unlink()
        before = {p.name: p.read_bytes() for p in self.sources.glob("*")}
        with self.assertRaises(SourceError):
            activate({self.source.id: wrong}, [self.source.id], "apt")
        self.assertEqual({p.name: p.read_bytes() for p in self.sources.glob("*")}, before)
        activate({self.source.id: self.source.to_dict()}, [self.source.id], "apt")
        owned = self.sources / (source_filename(self.source) + ".sources")
        self.assertTrue(owned.exists())
        activate({self.source.id: self.source.to_dict()}, [], "apt")
        self.assertFalse(owned.exists())
        self.assertEqual(Path("/etc/apt/sources.list").read_bytes(), self.main_text)
        hook_marker = self.root / "system-update-hook-ran"
        hook = Path("/etc/apt/apt.conf.d/99envy-e2e-hook")
        hook.write_text(f'APT::Update::Post-Invoke {{ "touch {hook_marker}"; }};\n')
        with signed_options(self.source):
            pass
        self.assertFalse(hook_marker.exists(), "private index refresh executed a system update hook")
        signed = self.web / "vendor/InRelease"
        original = signed.read_bytes()
        try:
            signed.write_bytes(original.replace(b"SHA256:", b"SHA257:", 1))
            with self.assertRaises(SourceError):
                with signed_options(self.source):
                    self.fail("tampered index signature was accepted")
        finally:
            signed.write_bytes(original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["cli", "native"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    REPORT["suite"] = args.suite
    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            REPORT["checks"].append({"name": test.id(), "status": "passed"})
        def addFailure(self, test, error):
            super().addFailure(test, error)
            REPORT["checks"].append({"name": test.id(), "status": "failed"})
        def addError(self, test, error):
            super().addError(test, error)
            REPORT["checks"].append({"name": test.id(), "status": "error"})
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CliE2E if args.suite == "cli" else NativeE2E)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    REPORT["successful"] = result.wasSuccessful()
    if args.report:
        args.report.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
