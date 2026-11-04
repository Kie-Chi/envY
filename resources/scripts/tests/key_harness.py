"""Throwaway key repositories driven by fake, or real, age and sops.

Every loaded envy module that bound a key path or run_cmd from envy.utils is
redirected to a temporary checkout and home, so key tests can never touch the
real repository or device key. In fake mode the identity AGE-SECRET-KEY-1<NAME>
has the recipient age1<name>, and encrypted files record their recipients. In
real mode the same scenarios run the installed age and sops binaries.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import yaml

from envy import utils
from envy.process import CommandError

MODULES = (
    "envy.key",
    "envy.config.cli",
    "envy.config.machine_nix",
    "envy.config.refine",
    "envy.config.secret_store",
    "envy.config.updates",
    "envy.git_commit",
    "envy.keys.commits",
    "envy.keys.identity",
    "envy.keys.importing",
    "envy.keys.operations",
    "envy.keys.prompts",
    "envy.keys.recovery",
    "envy.keys.rotation",
    "envy.keys.storage",
)
PATHS = (
    "ENVY_ROOT",
    "HOME_DIR",
    "AGE_KEY_DIR",
    "AGE_KEY_FILE",
    "SOPS_YAML",
    "SECRETS_DIR",
    "SECRETS_FILE",
    "RECOVERY_KEY_FILE",
    "DEVICE_LABEL_FILE",
)
KEY_FILES = ("SOPS_YAML", "SECRETS_FILE", "RECOVERY_KEY_FILE", "AGE_KEY_FILE", "DEVICE_LABEL_FILE")
TOOLS = {"age", "age-keygen", "sops", "ssh-to-age"}
REAL_TOOLS = ("age", "age-keygen", "sops")
PRIVATE_PREFIX = "AGE-SECRET-KEY-1"
AGE_HEADER = "fake-age-encryption\n"
_RECIPIENT = re.compile(r"age1[0-9a-z]+")


def real_tools_available() -> bool:
    return all(shutil.which(tool) for tool in REAL_TOOLS)


@dataclass(frozen=True)
class Identity:
    private: str
    public: str


def _failure(cmd: list[str], message: str) -> CommandError:
    return CommandError(subprocess.CompletedProcess(cmd, 1, "", message))


class KeyWorld:
    """One temporary checkout, device key directory, and age/sops backend."""

    def __init__(self, test, *, real: bool = False):
        if real and not real_tools_available():
            test.skipTest("age, age-keygen, and sops are not installed")
        if getattr(utils.run_cmd, "__module__", "") != "envy.utils":
            raise RuntimeError("a key world is already active")
        self.real = real
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "repo"
        self.home = base / "home"
        self.tmp = base / "tmp"
        # User-supplied files (key files to import, export targets) live
        # outside the checkout and home, so build() keeps them.
        self.inputs = base / "inputs"
        age_dir = self.home / ".config" / "sops" / "age"
        self.paths = {
            "ENVY_ROOT": self.root,
            "HOME_DIR": self.home,
            "AGE_KEY_DIR": age_dir,
            "AGE_KEY_FILE": age_dir / "keys.txt",
            "SOPS_YAML": self.root / ".sops.yaml",
            "SECRETS_DIR": self.root / "secrets",
            "SECRETS_FILE": self.root / "secrets" / "secrets.yaml",
            "RECOVERY_KEY_FILE": self.root / "secrets" / "recovery-key.age",
            "DEVICE_LABEL_FILE": self.root / ".device-label",
        }
        self.age_key = self.paths["AGE_KEY_FILE"]
        self.sops_yaml = self.paths["SOPS_YAML"]
        self.secrets = self.paths["SECRETS_FILE"]
        self.recovery_file = self.paths["RECOVERY_KEY_FILE"]
        self.identities: dict[str, Identity] = {}
        self.generated: list[Identity] = []
        self.calls: list[list[str]] = []
        self.tool_calls = 0
        self.fail_at: int | None = None
        self.failed: list[str] | None = None
        self._generated_names = 0
        self._real_run = utils.run_cmd
        self._runner = self.run_cmd
        for path in (self.root, self.home, self.tmp, self.inputs):
            path.mkdir()
        # Console output and printed keys are kept for assertions, not shown.
        self.output = io.StringIO()
        test.enterContext(redirect_stdout(self.output))
        test.enterContext(redirect_stderr(self.output))
        self._redirect(test)

    # ------------------------------------------------------------------
    # Redirection
    # ------------------------------------------------------------------

    def _redirect(self, test) -> None:
        for name in MODULES:
            importlib.import_module(name)
        originals = {name: getattr(utils, name) for name in PATHS}

        def start(patcher):
            patcher.start()
            test.addCleanup(patcher.stop)

        for module_name, module in list(sys.modules.items()):
            if module is None or not (module_name == "envy" or module_name.startswith("envy.")):
                continue
            for name, original in originals.items():
                if getattr(module, name, None) == original:
                    start(patch.object(module, name, self.paths[name]))
            if getattr(module, "run_cmd", None) is self._real_run:
                start(patch.object(module, "run_cmd", self._runner))
        # Real sops also loads keys from the user config directory, so neither
        # the real one nor the temporary device key there may be visible.
        start(patch.dict(os.environ, {
            "TMPDIR": str(self.tmp),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.inputs.parent / "xdg-config"),
        }))
        for name in ("SOPS_AGE_KEY", "SOPS_AGE_KEY_CMD", "SOPS_AGE_SSH_PRIVATE_KEY_FILE"):
            os.environ.pop(name, None)  # restored with the rest of os.environ
        start(patch.object(tempfile, "tempdir", str(self.tmp)))

    # ------------------------------------------------------------------
    # Repository construction and inspection
    # ------------------------------------------------------------------

    def identity(self, name: str) -> Identity:
        """Return the identity known by name, creating it on first use."""
        if name not in self.identities:
            if self.real:
                output = self._real_run(["age-keygen"], capture=True)
                private = next(line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY"))
                public = self._real_run(["age-keygen", "-y"], stdin_data=private + "\n", capture=True)
                self.identities[name] = Identity(private, public)
            else:
                self.identities[name] = Identity(PRIVATE_PREFIX + name.upper(), "age1" + name.lower())
        return self.identities[name]

    def install(self, *identities: Identity) -> None:
        """Make identities the local key file, the last one active."""
        directory = self.age_key.parent
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.age_key.write_text("".join(identity.private + "\n" for identity in identities))
        self.age_key.chmod(0o600)

    def build(
        self,
        *,
        devices: tuple[str, ...] = ("laptop", "desk"),
        local: str | None = "laptop",
        label: str | None = "laptop",
        recovery: bool = True,
        sealed: bool = True,
        secrets: dict | None = None,
    ) -> None:
        """Reset the checkout to a consistent repository with these keys."""
        from envy.keys import recovery as key_recovery
        from envy.keys import storage

        for path in (self.root, self.home, self.tmp):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        self.paths["SECRETS_DIR"].mkdir()
        keys = {name: self.identity(name).public for name in devices}
        if recovery:
            keys["recovery"] = self.identity("recovery").public
        storage.write_sops_yaml_keys(keys)
        if label:
            utils.write_device_metadata(sops_label=label)
        if local:
            self.install(self.identity(local))
        document = {"home": {"passwd": "hunter2"}} if secrets is None else secrets
        self._write_sops(self.secrets, list(keys.values()), yaml.safe_dump(document))
        if recovery and sealed:
            key_recovery.write_encrypted_recovery_key(self.identity("recovery").private, keys.values())
        self.calls.clear()
        self.tool_calls = 0
        self.fail_at = None
        self.failed = None

    def keys(self) -> dict[str, str]:
        from envy.keys import storage

        return storage.read_sops_yaml_keys()

    def local_publics(self) -> list[str]:
        if not self.age_key.exists():
            return []
        return self._dispatch(["age-keygen", "-y", str(self.age_key)]).splitlines()

    def known(self) -> list[Identity]:
        return [*self.identities.values(), *self.generated]

    def can_open(self, path: Path, identity: Identity) -> bool:
        with tempfile.NamedTemporaryFile("w", dir=self.tmp.parent, suffix=".txt", delete=False) as handle:
            handle.write(identity.private + "\n")
        key_file = Path(handle.name)
        try:
            if path == self.secrets:
                self._dispatch(["sops", "--decrypt", str(path)], age_key_file=key_file)
            else:
                self._dispatch(["age", "--decrypt", "-i", str(key_file), str(path)])
            return True
        except subprocess.CalledProcessError:
            return False
        finally:
            key_file.unlink()

    def read_secrets(self, identity: Identity) -> dict:
        with tempfile.NamedTemporaryFile("w", dir=self.tmp.parent, suffix=".txt", delete=False) as handle:
            handle.write(identity.private + "\n")
        try:
            return yaml.safe_load(
                self._dispatch(["sops", "--decrypt", str(self.secrets)], age_key_file=Path(handle.name))
            )
        finally:
            Path(handle.name).unlink()

    def read_recovery(self, identity: Identity) -> str:
        with tempfile.NamedTemporaryFile("w", dir=self.tmp.parent, suffix=".txt", delete=False) as handle:
            handle.write(identity.private + "\n")
        try:
            return self._dispatch(["age", "--decrypt", "-i", handle.name, str(self.recovery_file)])
        finally:
            Path(handle.name).unlink()

    def snapshot(self) -> dict[str, tuple[bool, bytes, int]]:
        result = {}
        for name in KEY_FILES:
            path = self.paths[name]
            result[name] = (
                (True, path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else (False, b"", 0)
            )
        return result

    def leftovers(self) -> list[str]:
        """Temporary or plaintext files a finished operation must not leave."""
        expected = {
            self.sops_yaml, self.secrets, self.recovery_file, self.paths["DEVICE_LABEL_FILE"],
            self.root / ".envy-key.lock", self.age_key,
            # Deliberate private backups made before replacing key files.
            self.sops_yaml.with_name(".sops.yaml.bak"),
        }
        found = []
        for base in (self.root, self.home, self.tmp):
            for path in base.rglob("*"):
                if path.is_file() and path not in expected and not path.name.startswith("keys.txt.bak"):
                    found.append(str(path.relative_to(base.parent)))
        return sorted(found)

    def assert_consistent(self, test) -> None:
        """Exactly the registered identities open the secrets and recovery key."""
        keys = self.keys()
        test.assertFalse(
            [label for label in keys if label == "recovery_new" or label.startswith("envy_rotation_old_")],
            keys,
        )
        registered = set(keys.values())
        for identity in self.known():
            for path in (self.secrets, self.recovery_file):
                # Whoever holds the recovery identity needs no recovery-key.age.
                if path == self.recovery_file and identity.public == keys.get("recovery"):
                    continue
                if path.exists():
                    test.assertEqual(
                        self.can_open(path, identity), identity.public in registered,
                        f"{path.name} for {identity.public}",
                    )
        local = self.local_publics()
        test.assertTrue(local and local[-1] in registered, "the active local key is registered")

    # ------------------------------------------------------------------
    # run_cmd replacement
    # ------------------------------------------------------------------

    def run_cmd(self, cmd, stdin_data=None, check=True, cwd=None, capture=True, activity=None, age_key_file=None):
        cmd = [str(part) for part in cmd]
        self.calls.append(cmd)
        if cmd[0] in TOOLS:
            self.tool_calls += 1
            if self.fail_at == self.tool_calls:
                self.failed = cmd
                raise _failure(cmd, "injected failure")
        output = self._dispatch(cmd, stdin_data=stdin_data, check=check, age_key_file=age_key_file)
        if cmd == ["age-keygen"]:
            private = next(line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY"))
            self.generated.append(Identity(private, self._dispatch(["age-keygen", "-y"], stdin_data=private)))
        return output

    def _dispatch(self, cmd, *, stdin_data=None, check=True, age_key_file=None) -> str:
        if cmd[0] not in TOOLS | {"hostname"}:
            raise AssertionError(f"unexpected command in a key test: {cmd}")
        if self.real:
            return self._real_run(cmd, stdin_data=stdin_data, check=check, capture=True, age_key_file=age_key_file)
        try:
            return self._fake(cmd, stdin_data or "", age_key_file)
        except CommandError:
            if check:
                raise
            return ""

    def _fake(self, cmd: list[str], stdin: str, age_key_file) -> str:
        program, args = cmd[0], cmd[1:]
        if program == "hostname":
            return "testhost"
        if program == "age-keygen" and not args:
            self._generated_names += 1
            identity = self.identity(f"gen{self._generated_names}")
            return f"# created: now\n# public key: {identity.public}\n{identity.private}"
        if program == "age-keygen" and args[0] == "-y":
            text = Path(args[1]).read_text() if len(args) > 1 else stdin
            return "\n".join(self._publics(text, cmd))
        if program == "age" and args[0] == "--encrypt":
            recipients = [args[index + 1] for index, arg in enumerate(args) if arg == "-r"]
            if not recipients or not all(_RECIPIENT.fullmatch(value) for value in recipients):
                raise _failure(cmd, "invalid recipient")
            output = args[args.index("-o") + 1]
            if output != "/dev/null":
                Path(output).write_text(AGE_HEADER + json.dumps({"recipients": recipients, "payload": stdin}))
            return ""
        if program == "age" and args[0] == "--decrypt":
            identities = self._publics(Path(args[args.index("-i") + 1]).read_text(), cmd)
            text = Path(args[-1]).read_text()
            if not text.startswith(AGE_HEADER):
                raise _failure(cmd, "not an age file")
            document = json.loads(text[len(AGE_HEADER):])
            if not set(identities) & set(document["recipients"]):
                raise _failure(cmd, "no identity matched any of the recipients")
            return document["payload"].strip()
        if program == "sops" and "--encrypt" in args:
            recipients = args[args.index("--age") + 1].split(",")
            self._write_sops(Path(args[args.index("--output") + 1]), recipients, Path(args[-1]).read_text())
            return ""
        if program == "sops" and args[0] == "--decrypt":
            key_file = Path(age_key_file or utils.AGE_KEY_FILE)
            if not key_file.exists():
                raise _failure(cmd, "no age identity")
            identities = self._publics(key_file.read_text(), cmd)
            data = yaml.safe_load(Path(args[1]).read_text())
            blob = json.loads(base64.b64decode(data["data"][len("ENC[FAKE,data:"):-len(",type:str]")]))
            if not set(identities) & set(blob["recipients"]):
                raise _failure(cmd, "no identity matched any of the recipients")
            return blob["plaintext"].strip()
        if program == "ssh-to-age":
            name = Path(args[args.index("-i") + 1]).read_text().split()[-1]
            return self.identity(name).private
        raise AssertionError(f"unexpected command in a key test: {cmd}")

    def _publics(self, text: str, cmd: list[str]) -> list[str]:
        publics = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith(PRIVATE_PREFIX):
                raise _failure(cmd, "malformed identity")
            publics.append("age1" + line[len(PRIVATE_PREFIX):].lower())
        return publics

    def _write_sops(self, output: Path, recipients: list[str], plaintext: str) -> None:
        if self.real:
            source = self.tmp / "build-plain.yaml"
            source.write_text(plaintext)
            try:
                self._real_run([
                    "sops", "--encrypt", "--config", "/dev/null", "--age", ",".join(recipients),
                    "--output", str(output), str(source),
                ])
            finally:
                source.unlink()
            return
        blob = base64.b64encode(json.dumps({"recipients": recipients, "plaintext": plaintext}).encode()).decode()
        output.write_text(yaml.safe_dump({
            "data": f"ENC[FAKE,data:{blob},type:str]",
            "sops": {"mac": "ENC[FAKE]", "age": [{"recipient": value, "enc": "fake"} for value in recipients]},
        }))
