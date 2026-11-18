#!/usr/bin/env python3
"""Exercise real Git/Nix source visibility using only synthetic temporary data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

OUTPUT_LIMIT = 131072


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def executable(value: str) -> str:
    found = shutil.which(value)
    if not found:
        raise ValueError(f"required tool unavailable: {value}")
    return str(Path(found).absolute())


def isolated_environment(root: Path, tools: list[str]) -> dict[str, str]:
    """Construct a fresh environment rather than filtering inherited credentials."""
    home = root / "home"
    config = root / "config"
    for directory in (home, config, root / "cache", root / "tmp", root / "git-template"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (config / "nix.conf").write_text("")
    (config / "registry.json").write_text('{"version":2,"flakes":[]}\n')
    return {
        "HOME": str(home), "USER": "source-fixture", "LOGNAME": "source-fixture",
        "PATH": os.pathsep.join(dict.fromkeys(str(Path(t).parent) for t in tools)),
        "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(config), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"), "LC_ALL": "C", "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_TEMPLATE_DIR": str(root / "git-template"),
        "GIT_AUTHOR_NAME": "Synthetic Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        "NIX_CONF_DIR": str(config), "NIX_USER_CONF_FILES": str(config / "nix.conf"),
        "NIX_CONFIG": "experimental-features = nix-command flakes\nflake-registry = "
                      + str(config / "registry.json")
                      + "\nsubstituters =\ntrusted-substituters =\nbuilders =\n",
        "NIX_REMOTE": f"local?root={root / 'store'}",
        "NIX_PATH": "", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "ENVY_FLAKE_SOURCE_MIRROR": "0", "ENVY_NOTIFY": "0", "ENVY_ACTIVITY": "0",
    }


class Driver:
    def __init__(self, root: Path, nix: str, git: str, python: str, timeout: int):
        self.root = root
        self.nix = nix
        self.git = git
        self.python = python
        self.timeout = timeout
        self.env = isolated_environment(root, [nix, git, python])
        self.commands: list[dict] = []
        self.cases: list[dict] = []

    def run(self, args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> dict:
        started = time.monotonic()
        record = {"id": len(self.commands) + 1, "argv": args, "cwd": str(cwd or self.root),
                  "startedAt": utc_now(), "executed": False, "stdinPolicy": "closed"}
        stdout, stderr = bytearray(), bytearray()
        try:
            process = subprocess.Popen(args, cwd=cwd or self.root, env=env or self.env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            record["executed"] = True
            selector = selectors.DefaultSelector()
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, stdout)
            selector.register(process.stderr, selectors.EVENT_READ, stderr)
            failure = None
            while selector.get_map():
                if time.monotonic() - started > self.timeout:
                    failure = "timeout"
                    break
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    target = key.data
                    target.extend(chunk[:max(0, OUTPUT_LIMIT - len(target))])
                    if len(target) >= OUTPUT_LIMIT:
                        failure = "output-limit"
                        break
                if failure:
                    break
            if failure:
                os.killpg(process.pid, signal.SIGKILL)
            selector.close()
            process.wait(timeout=5)
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            record.update(returncode=process.returncode, error=failure,
                          stdout=stdout.decode("utf-8", "replace"),
                          stderr=stderr.decode("utf-8", "replace"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.update(returncode=None, error=str(exc), stdout="", stderr="")
        record["durationSeconds"] = round(time.monotonic() - started, 4)
        self.commands.append(record)
        return record

    def require(self, result: dict) -> str:
        if result["returncode"] != 0 or result["error"]:
            raise RuntimeError(f"command {result['id']} failed: {result['error'] or result['stderr']}")
        return result["stdout"]

    def git_run(self, repo: Path, *args: str) -> dict:
        return self.run([self.git, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                         "-C", str(repo), *args])

    def nix_run(self, *args: str) -> dict:
        return self.run([self.nix, "--store", self.env["NIX_REMOTE"],
                         "--extra-experimental-features", "nix-command flakes", "--offline",
                         "--option", "accept-flake-config", "false",
                         "--option", "substituters", "", "--option", "builders", "", *args])

    def eval(self, ref: str, attr: str) -> dict:
        return self.nix_run("eval", "--no-write-lock-file", "--no-update-lock-file",
                            "--json", f"{ref}#sourceProbe.{attr}")

    def check(self, name: str, function, proof: str = "minimal-source"):
        start = len(self.commands)
        case = {"name": name, "proofLevel": proof, "startedAt": utc_now()}
        try:
            evidence = function()
            case.update(status="passed", evidence=evidence)
        except (AssertionError, RuntimeError, OSError, ValueError, KeyError) as exc:
            case.update(status="failed", error=str(exc) or type(exc).__name__)
        case["commandIds"] = [c["id"] for c in self.commands[start:]]
        self.cases.append(case)

    def expect_value(self, ref: str, attr: str, expected):
        result = self.eval(ref, attr)
        actual = json.loads(self.require(result))
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, observed {actual!r}")
        return {"actual": actual, "commandId": result["id"]}

    def expect_missing(self, ref: str, attr: str, filename: str):
        files_result = self.eval(ref, "files")
        files = json.loads(self.require(files_result))
        if filename in files:
            raise AssertionError(f"unexpected visible file: {filename}")
        result = self.eval(ref, attr)
        result["expectedFailure"] = True
        if result["returncode"] in (0, None) or result["error"] or filename not in result["stderr"]:
            raise AssertionError(f"expected Nix missing-file error for {filename}; command {result['id']}")
        return {"visibleFiles": files, "failureCommandId": result["id"],
                "expectedFailure": True, "actualError": result["stderr"]}

    def baseline(self, repo: Path):
        repo.mkdir(mode=0o700)
        self.require(self.git_run(repo, "init", "--initial-branch=fixture", "--template=" + str(self.root / "git-template")))
        shutil.copyfile(Path(__file__).parent / "source-fixture" / "flake.nix", repo / "flake.nix")
        (repo / "tracked.txt").write_text("tracked-v1\n")
        (repo / "deleted.txt").write_text("deleted-v1\n")
        (repo / ".gitignore").write_text("ignored-sentinel\n.device-label\n")
        self.require(self.git_run(repo, "add", "--", "flake.nix", "tracked.txt", "deleted.txt", ".gitignore"))
        self.require(self.git_run(repo, "commit", "--no-gpg-sign", "-m", "Create synthetic baseline"))

    def freeze(self, ref: str) -> dict:
        metadata = json.loads(self.require(self.nix_run("flake", "metadata", "--json",
                                                       "--no-write-lock-file", "--no-update-lock-file", ref)))
        logical = Path(metadata["path"])
        physical = self.root / "store" / logical.relative_to("/")
        if not physical.is_dir():
            raise AssertionError(f"rooted store source missing: {physical}")
        nar = self.require(self.nix_run("hash", "path", "--sri", str(physical))).strip()
        info_result = self.nix_run("path-info", "--json", str(logical))
        info = json.loads(self.require(info_result))
        if isinstance(info, list):
            store_info = info[0]
            info_shape = "list"
        elif isinstance(info, dict) and str(logical) in info:
            store_info = info[str(logical)]
            info_shape = "path-keyed-object"
        else:
            raise AssertionError("unknown nix path-info JSON shape")
        if nar != store_info["narHash"]:
            raise AssertionError("frozen source NAR differs from registered store NAR")
        locked_nar = metadata["locked"].get("narHash")
        if locked_nar is not None and nar != locked_nar:
            raise AssertionError("frozen source NAR differs from flake locked NAR")
        manifest = content_manifest(physical)
        return {"descriptorSource": {key: metadata[key] for key in ("original", "resolved")},
                "nixMetadataCompatibility": {"lockedNarHashPresent": locked_nar is not None,
                                             "pathInfoJsonShape": info_shape,
                                             "narOracle": "registered-store-path-info"},
                "frozenSource": {"logicalPath": str(logical), "physicalPath": str(physical),
                                 "narHash": nar, "files": manifest,
                                 "contentSha256": manifest_hash(manifest)},
                "metadata": metadata}


def content_manifest(directory: Path) -> list[dict]:
    result = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            result.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            result.append({"path": relative, "type": "file", "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "executable": bool(path.stat().st_mode & 0o111)})
        elif path.is_dir():
            result.append({"path": relative, "type": "directory"})
        else:
            raise ValueError(f"unsupported synthetic source entry: {relative}")
    return result


def manifest_hash(manifest: list[dict]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_matrix(driver: Driver) -> dict:
    repo = driver.root / "source"
    driver.baseline(repo)
    baseline_revision = driver.require(driver.git_run(repo, "rev-parse", "HEAD")).strip()
    git_ref, path_ref = "git+" + repo.as_uri(), "path:" + str(repo)
    (repo / "tracked.txt").write_text("tracked-v2\n")
    (repo / "deleted.txt").unlink()
    (repo / "new.nix").write_text('"synthetic-import"\n')
    (repo / "ignored-sentinel").write_text("synthetic-ignored-only\n")
    for source, ref in (("git", git_ref), ("path", path_ref)):
        driver.check("tracked-modification-" + source,
                     lambda ref=ref: driver.expect_value(ref, "tracked", "tracked-v2\n"))
        driver.check("tracked-deletion-" + source,
                     lambda ref=ref: driver.expect_value(ref, "deletedExists", False))
    before_ita = driver.require(driver.git_run(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    driver.check("untracked-import-git-hidden", lambda: driver.expect_missing(git_ref, "newImport", "new.nix"))
    driver.check("untracked-import-path-visible", lambda: driver.expect_value(path_ref, "newImport", "synthetic-import"))
    driver.require(driver.git_run(repo, "add", "--intent-to-add", "--", "new.nix"))
    after_ita = driver.require(driver.git_run(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    for source, ref in (("git", git_ref), ("path", path_ref)):
        driver.check("intent-to-add-import-" + source + "-visible",
                     lambda ref=ref: driver.expect_value(ref, "newImport", "synthetic-import"))
    driver.check("ignored-sentinel-git-hidden", lambda: driver.expect_missing(git_ref, "ignored", "ignored-sentinel"))
    driver.check("ignored-sentinel-path-visible", lambda: driver.expect_value(path_ref, "ignored", "synthetic-ignored-only\n"))
    frozen = {}
    for source, ref in (("git", git_ref), ("path", path_ref)):
        def freeze_case(source=source, ref=ref):
            value = driver.freeze(ref)
            frozen[source] = value
            entries = {entry["path"]: entry for entry in value["frozenSource"]["files"]}
            assert "new.nix" in entries and "deleted.txt" not in entries
            assert ("ignored-sentinel" in entries) == (source == "path")
            assert entries["tracked.txt"]["sha256"] == hashlib.sha256(b"tracked-v2\n").hexdigest()
            assert value["descriptorSource"]["original"]["type"] == source
            return value
        driver.check("freeze-nar-content-manifest-" + source, freeze_case)

    def frozen_stays():
        if set(frozen) != {"git", "path"}:
            raise AssertionError("both source freezes must complete")
        assert frozen["git"]["descriptorSource"] != frozen["path"]["descriptorSource"]
        assert frozen["git"]["frozenSource"]["narHash"] != frozen["path"]["frozenSource"]["narHash"]
        (repo / "tracked.txt").write_text("tracked-v3-after-freeze\n")
        (repo / "new.nix").write_text('"changed-after-freeze"\n')
        evidence = {}
        for source, value in frozen.items():
            original = value["frozenSource"]
            directory = Path(original["physicalPath"])
            actual = content_manifest(directory)
            assert actual == original["files"]
            assert (directory / "tracked.txt").read_text() == "tracked-v2\n"
            nar = driver.require(driver.nix_run("hash", "path", "--sri", str(directory))).strip()
            assert nar == original["narHash"]
            evidence[source] = {"narHash": nar, "contentSha256": manifest_hash(actual)}
        return evidence
    driver.check("frozen-sources-stable-after-live-mutation", frozen_stays)
    return {"refs": {"git": git_ref, "path": path_ref}, "baselineRevision": baseline_revision,
            "gitStatusBeforeIntentToAdd": before_ita, "gitStatusAfterIntentToAdd": after_ita,
            "intentToAddExpectedVisibility": True,
            "sourceFixture": "synthetic-only; no personal history"}


def fixture_path(temp_root, raw_path, label):
    """Normalize only the trusted temporary prefix, then reject every inner link."""
    supplied_root = Path(temp_root)
    temporary = supplied_root.resolve()
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(label + " is not an absolute fixture path")
    if path.is_relative_to(supplied_root):
        path = temporary / path.relative_to(supplied_root)
    elif not path.is_relative_to(temporary):
        raise RuntimeError(label + " escaped the private temporary root")
    cursor = temporary
    for component in path.relative_to(temporary).parts:
        cursor = cursor / component
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            # A missing parent cannot contain an existing link or file.
            break
        if stat.S_ISLNK(mode):
            raise RuntimeError(label + " contains a symlink inside the temporary root")
        if cursor != path and not stat.S_ISDIR(mode):
            raise RuntimeError(label + " has a non-directory parent")
    return path


def fixture_context(temp_root, expected_root, expected_home, source_checkout):
    temporary, forbidden = Path(temp_root).resolve(), Path(source_checkout).resolve()
    if temporary == forbidden or temporary.is_relative_to(forbidden):
        raise RuntimeError("temporary root must be outside the source checkout")
    repository = fixture_path(temp_root, expected_root, "fixture root")
    home = fixture_path(temp_root, expected_home, "fixture HOME")
    for label, path in (("fixture root", repository), ("fixture HOME", home)):
        if path == temporary or path.is_relative_to(forbidden):
            raise RuntimeError(label + " escaped the private temporary root")
        if not path.is_dir():
            raise RuntimeError(label + " is not an existing directory")
    return repository, home


def guard_fixture_request(temp_root, expected_root, expected_home, source_checkout, cwd, environment):
    repository, home = fixture_context(temp_root, expected_root, expected_home, source_checkout)
    if fixture_path(temp_root, cwd, "fixture cwd") != repository:
        raise RuntimeError("foreign cwd refused before production imports")
    if environment.get("ENVY_ROOT") != str(repository) or environment.get("HOME") != str(home):
        raise RuntimeError("fixture environment does not select the expected root/HOME")
    flake = fixture_path(temp_root, repository / "flake.nix", "flake marker")
    modules = fixture_path(temp_root, repository / "resources/scripts/envy", "module marker")
    if not flake.is_file() or not modules.is_dir():
        raise RuntimeError("fixture checkout markers missing before production imports")


def guard_resolved_targets(temp_root, expected_root, expected_home, source_checkout,
                           actual_root, actual_home, targets):
    repository, home = fixture_context(temp_root, expected_root, expected_home, source_checkout)
    if (fixture_path(temp_root, actual_root, "actual utils root") != repository
            or fixture_path(temp_root, actual_home, "actual utils HOME") != home):
        raise RuntimeError("actual utils root/HOME ignored fixture isolation")
    hosts = repository / "hosts"
    machines = hosts / ("darwin" if sys.platform == "darwin" else "linux")
    expected = {
        "host directory": hosts,
        "default host": hosts / "default.nix",
        "machine directory": machines,
        "device label": repository / ".device-label",
        "secret path (never opened)": repository / "secrets/secrets.yaml",
    }
    for variant in ("import", "copy"):
        for owner in ("host", "managed", "utils"):
            expected[owner + "/" + variant] = machines / ("source-" + variant + ".nix")
    for label, raw_path in targets.items():
        if label not in expected:
            raise RuntimeError("unknown production target role: " + label)
        path = fixture_path(temp_root, raw_path, label)
        if path != expected[label]:
            raise RuntimeError("production target differs from expected fixture role: " + label)


HOST_HELPER = r'''
import importlib.util
import json
import os
import sys
from pathlib import Path

mode, repository, home, temporary, checkout, driver_file = sys.argv[1:]
spec = importlib.util.spec_from_file_location("source_guard", driver_file)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
# Validate cwd, marker and caller selection before importing production modules.
guard.guard_fixture_request(temporary, repository, home, checkout, Path.cwd(), os.environ)
from envy import utils
guard.guard_resolved_targets(temporary, repository, home, checkout,
                             utils.ENVY_ROOT, utils.HOME_DIR, {})
from envy import host
from envy.config import machine_nix, refine

def check_targets():
    guard.guard_fixture_request(temporary, repository, home, checkout, Path.cwd(), os.environ)
    targets = {
        "host directory": host.HOSTS_DIR,
        "default host": host.DEFAULT_MACHINE,
        "machine directory": host.MACHINES_DIR,
        "device label": utils.DEVICE_LABEL_FILE,
        "secret path (never opened)": utils.SECRETS_FILE,
    }
    for variant in ("import", "copy"):
        identity = "source-" + variant
        targets["host/" + variant] = host.machine_file(identity)
        targets["managed/" + variant] = machine_nix.machine_config_file(identity)
        targets["utils/" + variant] = utils.machine_config_file(identity)
    # Both possible selections are checked explicitly; never resolve a current
    # machine implicitly, since that reads device metadata even with ENVY_MACHINE.
    if os.environ.get("ENVY_MACHINE") not in ("source-import", "source-copy"):
        raise RuntimeError("unknown fixture machine selection")
    guard.guard_resolved_targets(temporary, repository, home, checkout,
                                 utils.ENVY_ROOT, utils.HOME_DIR, targets)

check_targets()
commit_offers = []
def decline_commit(paths, message, **kwargs):
    check_targets()
    commit_offers.append({"decision": "No", "productionOfferExecuted": False,
                          "paths": [str(path) for path in paths]})
    return False
# Never enter production Git safety checks or read a secret document.
host.offer_mutation_commit = decline_commit
if mode in ("import", "copy"):
    check_targets()
    host.cmd_init(machine_id="source-" + mode, mode=mode, force=False)
    if len(commit_offers) != 1:
        raise RuntimeError("expected exactly one declined commit offer")
    print("SOURCE_RESULT=" + json.dumps({"mode": mode, "created": True,
                                          "commitOffers": commit_offers}))
elif mode == "refine":
    results = {}
    for variant in ("import", "copy"):
        os.environ["ENVY_MACHINE"] = "source-" + variant
        check_targets()
        utils.set_device_machine_id("source-" + variant)
        machine_nix.write_mirror_overrides({"envy.mirrors.overrides.fixture": "synthetic-endpoint"})
        report = refine.refine_agents(write=True, strict=True)
        check = refine.refine_agents(write=False, strict=True)
        if not report.ok or not check.ok:
            raise RuntimeError(str(report.errors + check.errors))
        results[variant] = {"changed": report.changed, "validated": check.ok}
    print("SOURCE_RESULT=" + json.dumps(results))
else:
    raise ValueError(mode)
'''


def host_matrix(driver: Driver, scripts: Path, nixpkgs_lib: Path):
    repo = driver.root / "host-source"
    driver.baseline(repo)
    (repo / "resources/scripts/envy").mkdir(parents=True)
    hosts = repo / "hosts"
    hosts.mkdir()
    default = hosts / "default.nix"
    default.write_text('{ ... }:\n{\n  envy.sourceFixtureVersion = "v1";\n}\n')
    driver.require(driver.git_run(repo, "add", "--", "hosts/default.nix"))
    driver.require(driver.git_run(repo, "commit", "--no-gpg-sign", "-m", "Add synthetic host default"))
    head = driver.require(driver.git_run(repo, "rev-parse", "HEAD")).strip()
    env = dict(driver.env, ENVY_ROOT=str(repo), PYTHONPATH=str(scripts), ENVY_MACHINE="source-import")
    helper = driver.root / "host_helper.py"
    helper.write_text(HOST_HELPER)
    helper_args = [str(repo), driver.env["HOME"], str(driver.root), str(scripts.parents[1]), str(Path(__file__).resolve())]

    def helper_run(mode):
        return driver.run([driver.python, str(helper), mode, *helper_args], cwd=repo, env=env)

    def create(mode):
        result = helper_run(mode)
        payload = json.loads(driver.require(result).split("SOURCE_RESULT=")[-1])
        actual = driver.require(driver.git_run(repo, "rev-parse", "HEAD")).strip()
        if actual != head:
            raise AssertionError("host CLI unexpectedly committed")
        offers = payload.get("commitOffers", [])
        if len(offers) != 1 or offers[0]["decision"] != "No" or offers[0]["productionOfferExecuted"]:
            raise AssertionError("commit offer must use the No stub")
        return {"headUnchanged": actual, "commitFollowup": "stubbed-No", "commandId": result["id"]}
    for mode in ("import", "copy"):
        driver.check("host-cli-init-" + mode + "-no-commit", lambda mode=mode: create(mode), "real-host-source-layer")

    def managed():
        result = helper_run("refine")
        payload = json.loads(driver.require(result).split("SOURCE_RESULT=")[-1])
        if payload != {"import": {"changed": True, "validated": True}, "copy": {"changed": True, "validated": True}}:
            raise AssertionError("both generated managed blocks must refine and validate")
        if driver.require(driver.git_run(repo, "rev-parse", "HEAD")).strip() != head:
            raise AssertionError("refine changed HEAD")
        return {"refine": payload, "headUnchanged": True}
    driver.check("host-managed-block-refine", managed, "real-host-source-layer")

    def evaluate(expected):
        platform = "darwin" if sys.platform == "darwin" else "linux"
        paths = {mode: str(hosts / platform / ("source-" + mode + ".nix")) for mode in ("import", "copy")}
        expression = ("let lib = import " + json.dumps(str(nixpkgs_lib))
                      + "; evaluate = path: (lib.evalModules { modules = [ "
                      + "{ options.envy = lib.mkOption { type = lib.types.attrs; }; } "
                      + "(builtins.toPath path) ]; }).config.envy; in { "
                      + "; ".join(mode + " = evaluate " + json.dumps(path) for mode, path in paths.items()) + "; }")
        result = driver.nix_run("eval", "--impure", "--json", "--expr", expression)
        values = json.loads(driver.require(result))
        versions = {mode: value["sourceFixtureVersion"] for mode, value in values.items()}
        if versions != expected:
            raise AssertionError(f"expected {expected!r}, observed {versions!r}")
        for value in values.values():
            if value["mirrors"]["overrides"]["fixture"] != "synthetic-endpoint" or "agents" not in value:
                raise AssertionError("generated managed blocks were not evaluated")
        return {"evaluatedVersions": versions, "moduleEngine": str(nixpkgs_lib), "managedBlocksEvaluated": True}
    driver.check("host-import-copy-default-v1", lambda: evaluate({"import": "v1", "copy": "v1"}), "real-host-source-layer")
    default.write_text('{ ... }:\n{\n  envy.sourceFixtureVersion = "v2";\n}\n')
    driver.check("host-import-follows-copy-retains-v1", lambda: evaluate({"import": "v2", "copy": "v1"}), "real-host-source-layer")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--nix", default="nix")
    result.add_argument("--git", default="git")
    result.add_argument("--python", default=sys.executable)
    result.add_argument("--scripts", type=Path, required=True, help="envY resources/scripts directory")
    result.add_argument("--nixpkgs-lib", type=Path, required=True, help="explicit locked nixpkgs lib directory")
    result.add_argument("--timeout", type=int, default=45)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    report = {"schemaVersion": 1, "startedAt": utc_now(), "status": "unavailable", "cases": [],
              "commands": [], "unknown": [], "limitations": [
                  "Source matrix uses a synthetic flake with no remote inputs.",
                  "Host proof uses production creation/refine interfaces and nixpkgs evalModules with freeform test data; full system policy/apply requires the later VM stage.",
                  "Host commit follow-up uses an explicit No stub; production commit safety checks and interactive setup UI are outside this check.",
              ]}
    try:
        if not __debug__:
            raise ValueError("this regression driver must run without Python optimization")
        if not 1 <= args.timeout <= 120:
            raise ValueError("--timeout must be between 1 and 120 seconds")
        scripts, library = args.scripts.resolve(), args.nixpkgs_lib.resolve()
        if not (scripts / "envy" / "host.py").is_file():
            raise ValueError("--scripts must contain envy/host.py")
        if not (library / "modules.nix").is_file() or not (library / "default.nix").is_file():
            raise ValueError("--nixpkgs-lib must contain modules.nix and default.nix")
        nix, git, python = (executable(value) for value in (args.nix, args.git, args.python))
        with tempfile.TemporaryDirectory(prefix="envy-source-visibility-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            driver = Driver(root, nix, git, python, args.timeout)
            report["temporaryRoot"] = str(root)
            report["commands"] = driver.commands
            report["cases"] = driver.cases
            report["tools"] = {}
            for tool, command in (("nix", [nix, "--version"]), ("git", [git, "--version"]), ("python", [python, "--version"])):
                version = driver.run(command)
                report["tools"][tool] = {"path": command[0], "version": driver.require(version).strip(), "commandId": version["id"]}
            report["libSource"] = {"path": str(library), "narHash": driver.require(driver.nix_run("hash", "path", "--sri", str(library))).strip(), "provenance": "explicit caller-provided locked input"}
            report["sourceMatrix"] = source_matrix(driver)
            host_matrix(driver, scripts, library)
            report["status"] = "passed" if all(case["status"] == "passed" for case in driver.cases) else "failed"
    except (ValueError, RuntimeError, OSError) as exc:
        report["error"] = str(exc)
        report["unknown"].append("remaining cases not executed after infrastructure failure")
    report["finishedAt"] = utc_now()
    report["actualCaseCount"] = len(report["cases"])
    report["executedCommandCount"] = sum(command["executed"] for command in report["commands"])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
