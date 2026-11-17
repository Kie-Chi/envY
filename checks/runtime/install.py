#!/usr/bin/env python3
"""Closed offline official Nix installation probe, composed with the boot driver."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import time

import vm

NIX_VERSION = "2.33.1"
NIX_TAR_SHA256 = "4cc8606dcf5c315b40197a394456a6dedbb47a3403030b632371470c34c8735d"
NIX_TAR_SIZE = 24662660
MAX_INSTALL_TIMEOUT = 1200
VERIFY_TIMEOUT = 120
RECEIPT_PREFIX = "ENVY_RUNTIME_INSTALL_RECEIPT "
STAGES = ("bootstrap", "initial-no-nix", "archive-hash", "archive-members", "install",
          "client-version", "daemon-rpc", "daemon-service", "build-users", "profile")
TOOLS = ("python3", "bash", "sudo", "tar", "xz", "curl", "git", "systemctl",
         "cp", "groupadd", "useradd", "getent")
REQUIRED_TOOLS = tuple(tool for tool in TOOLS if tool not in ("curl", "git"))


@dataclass(frozen=True)
class InstallConfig(vm.BootConfig):
    nix_tar: str | None = None
    install_timeout: float = MAX_INSTALL_TIMEOUT


# This program is delivered only to the fresh synthetic guest. Its only commands
# and paths are fixed here; no caller can supply a guest command or source tree.
GUEST_PROGRAM = r'''#!/usr/bin/python3
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import pwd
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

TOKEN = @TOKEN@
EXPECTED_SHA256 = @SHA256@
EXPECTED_SIZE = @SIZE@
INSTALL_TIMEOUT = @INSTALL_TIMEOUT@
RECEIPT_PREFIX = @PREFIX@
STAGES = @STAGES@
TOOLS = @TOOLS@
REQUIRED_TOOLS = @REQUIRED_TOOLS@
ARCHIVE_PREFIX = "nix-2.33.1-x86_64-linux"
NIX = "/nix/var/nix/profiles/default/bin/nix"
ENV = {"HOME": "/home/envy-runtime", "USER": "envy-runtime", "LOGNAME": "envy-runtime",
       "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TERM": "dumb",
       "NIX_USER_CONF_FILES": "/dev/null", "NIX_PATH": ""}
OUTPUT_LIMIT = 2 * 1024 * 1024
output_used = 0


class GuestFailure(Exception):
    pass


def cancel(signum, frame):
    raise KeyboardInterrupt


def command(argv, timeout=30, echo=False):
    global output_used
    process = subprocess.Popen(argv, env=ENV, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    raise GuestFailure("guest-command-timeout")
                for key, _ in selector.select(0.05):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    allowed = max(0, OUTPUT_LIMIT - output_used)
                    bounded = data[:allowed]
                    output_used += len(bounded)
                    if key.fileobj is process.stdout:
                        output.extend(bounded)
                    if echo:
                        sys.stdout.buffer.write(bounded)
                        sys.stdout.buffer.flush()
                    if len(data) > allowed:
                        raise GuestFailure("guest-output-limit")
            if process.wait(timeout=1) != 0:
                raise GuestFailure("guest-command-failed")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()
    return bytes(output)


def validate_members(members):
    if not 0 < len(members) <= 10000:
        raise GuestFailure("archive-members")
    names = {}
    total = 0
    for member in members:
        name = member.name.rstrip("/")
        if (not name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or str(PurePosixPath(name)) != name
                or any(p in ("", ".", "..") for p in name.split("/"))
                or not (name == ARCHIVE_PREFIX or name.startswith(ARCHIVE_PREFIX + "/"))
                or name in names or not (member.isfile() or member.isdir() or member.issym())
                or member.mode & 0o7000 or member.size < 0):
            raise GuestFailure("archive-members")
        total += member.size
        if total > 1024**3:
            raise GuestFailure("archive-size")
        names[name] = member
    for name, member in names.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) in names and not names[str(parent)].isdir():
                raise GuestFailure("archive-link-parent")
        if member.issym():
            target = member.linkname
            if not target or any(ord(c) < 32 or ord(c) == 127 for c in target):
                raise GuestFailure("archive-link")
            if target.startswith("/"):
                if not re.fullmatch(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+._-]+(?:/[A-Za-z0-9+._/-]+)?", target):
                    raise GuestFailure("archive-link")
                resolved = ARCHIVE_PREFIX + "/store/" + target[len("/nix/store/"):]
                if posixpath.normpath(resolved) != resolved:
                    raise GuestFailure("archive-link")
            else:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if not resolved.startswith(ARCHIVE_PREFIX + "/") or resolved not in names:
                raise GuestFailure("archive-link")
    installer = names.get(ARCHIVE_PREFIX + "/install")
    if installer is None or not installer.isfile():
        raise GuestFailure("archive-installer")


def user_probe():
    receipt = {"token": TOKEN, "arch": platform.machine(), "version": None,
               "initialNoNix": False, "daemon": False, "daemonService": False,
               "buildUsers": 0, "profile": False, "installerExitZero": False,
               "bootstrapTools": {tool: shutil.which(tool, path=ENV["PATH"]) is not None for tool in TOOLS},
               "stages": {stage: False for stage in STAGES}, "error": None}
    stage = "bootstrap"
    try:
        identity = pwd.getpwuid(os.getuid())
        if (identity.pw_name != "envy-runtime" or identity.pw_dir != "/home/envy-runtime"
                or os.getuid() == 0 or receipt["arch"] != "x86_64"
                or not all(receipt["bootstrapTools"][tool] for tool in REQUIRED_TOOLS)):
            raise GuestFailure("guest-prerequisites-unavailable")
        receipt["stages"][stage] = True
        stage = "initial-no-nix"
        receipt["initialNoNix"] = not os.path.lexists("/nix") and shutil.which("nix", path=ENV["PATH"]) is None
        if not receipt["initialNoNix"]:
            raise GuestFailure("initial-nix-present")
        receipt["stages"][stage] = True
        stage = "archive-hash"
        descriptor = os.open("/run/envy-runtime-install-seed/nix.tar.xz", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != EXPECTED_SIZE:
                raise GuestFailure("archive-size")
            digest = hashlib.sha256()
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != EXPECTED_SHA256:
                raise GuestFailure("archive-hash")
            receipt["stages"][stage] = True
            source.seek(0)
            stage = "archive-members"
            with tarfile.open(fileobj=source, mode="r:xz") as archive:
                members = archive.getmembers()
                validate_members(members)
                # Only a new guest-owned directory receives the fixed-hash archive.
                # Legitimate absolute /nix/store symlinks require trusted extraction.
                extraction = Path(tempfile.mkdtemp(prefix="envy-runtime-install-", dir="/tmp"))
                archive.extractall(extraction, members=members, filter="fully_trusted")
        receipt["stages"][stage] = True
        stage = "install"
        command(["/bin/bash", str(extraction / ARCHIVE_PREFIX / "install"),
                 "--daemon", "--yes", "--no-channel-add"], timeout=INSTALL_TIMEOUT, echo=True)
        receipt["installerExitZero"] = True
        receipt["stages"][stage] = True
        stage = "client-version"
        if command([NIX, "--version"]).strip() != b"nix (Nix) 2.33.1":
            raise GuestFailure("client-version")
        receipt["version"] = "2.33.1"
        receipt["stages"][stage] = True
        stage = "daemon-rpc"
        # Explicit daemon store forbids silently falling back to a local store.
        info = json.loads(command([NIX, "--extra-experimental-features", "nix-command",
                                   "--store", "daemon", "store", "info", "--json"]))
        if not isinstance(info, dict) or info.get("url") != "daemon" or info.get("version") != "2.33.1":
            raise GuestFailure("daemon-rpc")
        receipt["daemon"] = True
        receipt["stages"][stage] = True
        stage = "daemon-service"
        if command(["/usr/bin/systemctl", "is-active", "nix-daemon.service"]).strip() != b"active":
            raise GuestFailure("daemon-service")
        receipt["daemonService"] = True
        receipt["stages"][stage] = True
        stage = "build-users"
        group = grp.getgrnam("nixbld")
        users = [u for u in pwd.getpwall() if re.fullmatch(r"nixbld[0-9]+", u.pw_name)
                 and u.pw_uid > 0 and (u.pw_gid == group.gr_gid or u.pw_name in group.gr_mem)]
        receipt["buildUsers"] = len(users)
        if not users:
            raise GuestFailure("build-users")
        receipt["stages"][stage] = True
        stage = "profile"
        if command(["/bin/bash", "--noprofile", "--norc", "-c",
                    '. /etc/profile.d/nix.sh; command -v nix']).strip() != NIX.encode():
            raise GuestFailure("profile")
        receipt["profile"] = True
        receipt["stages"][stage] = True
    except KeyboardInterrupt:
        receipt["error"] = "cancelled:" + stage
    except (GuestFailure, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        receipt["error"] = "failed:" + stage
    print("\n" + RECEIPT_PREFIX + json.dumps(receipt, sort_keys=True), flush=True)


def main():
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    if os.getuid() != 0:
        user_probe()
        return
    # Cloud-init is root; the official installer itself runs as the synthetic user.
    with open("/dev/ttyS0", "wb", buffering=0) as serial:
        try:
            mountpoint = Path("/run/envy-runtime-install-seed")
            mountpoint.mkdir(mode=0o755, exist_ok=False)
            command(["/usr/bin/mount", "-o", "ro,nosuid,nodev,noexec", "/dev/sr0", str(mountpoint)])
            process = subprocess.Popen(["/usr/bin/sudo", "-n", "-u", "envy-runtime", "--",
                                        "/usr/bin/env", "-i", *[k + "=" + v for k, v in ENV.items()],
                                        "/usr/bin/python3", "/usr/local/lib/envy-runtime-install-probe.py"],
                                       env=ENV, stdin=subprocess.DEVNULL, stdout=serial, stderr=serial,
                                       start_new_session=True)
            try:
                process.wait(timeout=INSTALL_TIMEOUT + 120)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        finally:
            command(["/usr/bin/systemctl", "poweroff", "--no-block"])


if __name__ == "__main__":
    main()
'''


def guest_program(token: str, install_timeout: float) -> str:
    values = {"TOKEN": token, "SHA256": NIX_TAR_SHA256, "SIZE": NIX_TAR_SIZE,
              "INSTALL_TIMEOUT": install_timeout, "PREFIX": RECEIPT_PREFIX,
              "STAGES": list(STAGES), "TOOLS": list(TOOLS), "REQUIRED_TOOLS": list(REQUIRED_TOOLS)}
    program = GUEST_PROGRAM
    for key, value in values.items():
        program = program.replace("@" + key + "@", repr(value))
    return program


def seed_data(token: str, install_timeout: float) -> tuple[str, str]:
    program = guest_program(token, install_timeout)
    user_data = """#cloud-config
users:
  - name: envy-runtime
    homedir: /home/envy-runtime
    lock_passwd: true
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    shell: /bin/bash
ssh_pwauth: false
disable_root: true
package_update: false
package_upgrade: false
write_files:
  - path: /usr/local/lib/envy-runtime-install-probe.py
    owner: root:root
    permissions: '0755'
    content: """ + json.dumps(program) + """
runcmd:
  - [ /usr/bin/python3, /usr/local/lib/envy-runtime-install-probe.py ]
"""
    metadata = json.dumps({"instance-id": "envy-runtime-" + token, "local-hostname": "envy-runtime"}) + "\n"
    return user_data, metadata


@contextmanager
def verified_tar(config: InstallConfig, deadline: float):
    if config.nix_tar is None:
        raise vm.ProbeError("missing-nix-tar")
    path = vm.safe_path(config.nix_tar)
    if path.is_symlink() or path.resolve() != path:
        raise vm.ProbeError("tar-not-immutable-regular-file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size != NIX_TAR_SIZE):
            raise vm.ProbeError("tar-size-or-mode")
        digest = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise vm.ProbeError("timeout")
            digest.update(chunk)
        if digest.hexdigest() != NIX_TAR_SHA256:
            raise vm.ProbeError("tar-sha256-mismatch")
        source.seek(0)
        yield source


@contextmanager
def seed_payloads(work: Path, source, user_data: str, metadata: str, deadline: float, *, directory_fd=None):
    """Compatibility wrapper around the boot driver's explicit readonly grafts."""
    directory = (os.dup(directory_fd) if directory_fd is not None
                 else os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    try:
        payloads = (("user-data", user_data.encode()), ("meta-data", metadata.encode()),
                    ("network-config", b"version: 2\nethernets: {}\n"), ("nix.tar.xz", source))
        with vm.payload_grafts(directory, payloads, deadline) as prepared:
            yield prepared
    finally:
        os.close(directory)


pinned_work = vm.pinned_work


class InstallRunner(vm.Runner):
    """Preserve the install runner signature while keeping native cwd public."""

    def __init__(self, root, tools, deadline, limit, directory_fd, display_root, *, darwin_ps=None):
        super().__init__(display_root, tools, deadline, limit, directory_fd=directory_fd, darwin_ps=darwin_ps)


def receipt_from(raw: bytes, token: str) -> dict:
    receipts = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith(RECEIPT_PREFIX):
            try:
                receipts.append(json.loads(line[len(RECEIPT_PREFIX):]))
            except ValueError:
                raise vm.ProbeError("invalid-install-receipt") from None
    if len(receipts) != 1 or not isinstance(receipts[0], dict):
        raise vm.ProbeError("missing-or-mismatched-install-receipt")
    row = receipts[0]
    tools, stages = row.get("bootstrapTools"), row.get("stages")
    if (row.get("token") != token or row.get("arch") != "x86_64" or row.get("version") != NIX_VERSION
            or any(row.get(key) is not True for key in
                   ("initialNoNix", "daemon", "daemonService", "profile", "installerExitZero"))
            or type(row.get("buildUsers")) is not int or not 0 < row["buildUsers"] <= 128
            or not isinstance(stages, dict) or set(stages) != set(STAGES)
            or any(value is not True for value in stages.values())
            or not isinstance(tools, dict) or set(tools) != set(TOOLS)
            or any(type(value) is not bool for value in tools.values())
            or any(tools[tool] is not True for tool in REQUIRED_TOOLS)
            or row.get("error", "missing") is not None):
        raise vm.ProbeError("missing-or-mismatched-install-receipt")
    return row


def empty_report() -> dict:
    return {"schemaVersion": 1, "command": "install-probe", "status": "unavailable", "ok": False,
            "evidence": "linux-vm-nix-install-only", "envyInstall": False, "envyConfig": False,
            "envyApply": False, "requiresMissingNixHttpsBranch": False, "workdir": None,
            "receipt": None, "commands": [], "error": None, "rollbackClaimed": False}


def install_probe(config: InstallConfig) -> dict:
    report = empty_report()
    try:
        if (isinstance(config.install_timeout, bool) or not isinstance(config.install_timeout, (int, float))
                or not math.isfinite(config.install_timeout) or not 0 < config.install_timeout <= MAX_INSTALL_TIMEOUT):
            raise vm.ProbeError("invalid-install-timeout")
        if (isinstance(config.timeout, bool) or not isinstance(config.timeout, (int, float))
                or not math.isfinite(config.timeout) or not 0 < config.timeout <= vm.MAX_TIMEOUT):
            raise vm.ProbeError("invalid-timeout")
        deadline = time.monotonic() + config.timeout
        with vm.preflight(config, deadline) as (base_fd, work, tools), verified_tar(config, deadline) as source:
            work.mkdir(mode=0o700, exist_ok=False)
            report.update(status="failure", workdir=str(work))
            with pinned_work(work) as (directory_fd, root), \
                    vm.exclusive_file("overlay.qcow2", directory_fd=directory_fd) as overlay, \
                    vm.bound_file(directory_fd, "seed.iso") as (iso_writer, iso_readonly):
                runner = InstallRunner(root, tools, deadline, config.output_limit, directory_fd, work,
                                       darwin_ps=config.darwin_ps)
                report["commands"] = runner.commands
                qemu, qemu_img, mkisofs = tools
                base_path, base_fds = f"/dev/fd/{base_fd}", (base_fd,)
                vm.validate_image_info(runner.run("image-info", [qemu_img, "info", "--output=json", "-f", "qcow2", base_path],
                                                 capture=True, pass_fds=base_fds))
                token = secrets.token_hex(32)
                user_data, metadata = seed_data(token, config.install_timeout)
                overlay_fd, iso_fd = overlay.fileno(), iso_writer.fileno()
                overlay_path, disk_fds = f"/dev/fd/{overlay_fd}", (base_fd, overlay_fd)
                runner.run("overlay-create", [qemu_img, "create", "-f", "qcow2", "-F", "qcow2", "-b",
                                              base_path, overlay_path], pass_fds=disk_fds)
                runner.run("overlay-resize", [qemu_img, "resize", "-f", "qcow2", overlay_path, "40G"],
                           pass_fds=disk_fds)
                with seed_payloads(root, source, user_data, metadata, deadline,
                                   directory_fd=directory_fd) as (grafts, descriptors):
                    runner.run("seed-create", [mkisofs, "-quiet", "-output", f"/dev/fd/{iso_fd}", "-volid", "cidata",
                                               "-joliet", "-rock", "-file-mode", "0444", "-dir-mode", "0555", "-uid", "0", "-gid", "0",
                                               "-follow-links", "-graft-points", *grafts], pass_fds=(iso_fd, *descriptors))
                # Installation has its own budget after preparation and boot.
                runner.deadline = time.monotonic() + config.timeout + config.install_timeout + VERIFY_TIMEOUT
                raw = runner.run("qemu", vm.qemu_argv(qemu, base_fd, overlay_fd, iso_readonly), capture=True,
                                 pass_fds=(base_fd, overlay_fd, iso_readonly))
                report["receipt"] = receipt_from(raw, token)
                report.update(status="passed", ok=True)
    except KeyboardInterrupt:
        report.update(status="failure", error="cancelled")
    except vm.ProbeError as exc:
        report["error"] = str(exc)
    except (OSError, ValueError, subprocess.SubprocessError):
        report["error"] = "host-io-or-process-error"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = vm.JsonParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=vm.JsonParser)
    install = sub.add_parser("install-probe")
    install.add_argument("--workdir", required=True)
    for name in ("base", "sha256", "qemu", "qemu-img", "mkisofs", "nix-tar"):
        install.add_argument("--" + name)
    install.add_darwin_ps()
    install.add_argument("--timeout", type=float, default=vm.MAX_TIMEOUT)
    install.add_argument("--install-timeout", type=float, default=MAX_INSTALL_TIMEOUT)
    install.add_argument("--output-limit", type=int, default=vm.MAX_OUTPUT)
    previous = {}
    try:
        def cancel(signum, frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        args = vars(parser.parse_args(argv))
        args.pop("command")
        result = install_probe(InstallConfig(**args))
    except (vm.ProbeError, KeyboardInterrupt) as exc:
        result = empty_report()
        result["error"] = "cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
