"""Check a desktop package from a GUI session with an inherited Electron Node flag."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def check(package: Path) -> dict:
    package = package.resolve(strict=True)
    app = next((package / "Applications").glob("*.app"))
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert app.name == "T3 Code.app", "Application filename must be T3 Code.app"
    assert info["CFBundleName"] == "T3 Code", "Native bundle name must be T3 Code"
    assert info["CFBundleDisplayName"] == "T3 Code", "Native display name must be T3 Code"
    launcher = app / "Contents/MacOS" / info["CFBundleExecutable"]
    version = subprocess.run(
        [str(package / "bin/t3"), "--version"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip().removeprefix("t3 v")
    with tempfile.TemporaryDirectory(prefix="envy-t3-desktop-check-") as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {key: os.environ[key] for key in
               ["PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG"] if key in os.environ}
        env.update(HOME=str(home), T3CODE_HOME=str(root / "state"),
                   T3CODE_PORT=str(port), ELECTRON_RUN_AS_NODE="1", NO_COLOR="1")
        runtime_path = root / "state/userdata/server-runtime.json"
        runtime_path.parent.mkdir(parents=True)
        (runtime_path.parent / "client-settings.json").write_text(json.dumps({"confirmQuit": False}))
        runtime = None
        with (root / "desktop.log").open("w") as log:
            process = subprocess.Popen(
                [str(launcher), "--use-mock-keychain"], env=env, cwd=root, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = (root / "desktop.log").read_text(errors="replace")
                    output = re.sub(r"(?:https?|wss?)://[^\s]+", "[URL]", output)
                    raise RuntimeError("Desktop exited before readiness: " + output[-2400:])
                if runtime_path.exists():
                    runtime = json.loads(runtime_path.read_text())
                    if runtime["serverVersion"] != version:
                        raise RuntimeError("Desktop and CLI versions differ")
                    try:
                        with urllib.request.urlopen(runtime["origin"] + "/", timeout=2) as response:
                            window_created = "main window created" in (root / "desktop.log").read_text(errors="replace")
                            if response.status == 200 and window_created:
                                command = subprocess.run(
                                    ["ps", "-p", str(process.pid), "-o", "comm="],
                                    check=True, capture_output=True, text=True,
                                ).stdout.strip()
                                assert command == str((app / "Contents/MacOS/T3 Code").resolve()), command
                                query = ("ObjC.import('AppKit'); "
                                         "const a=$.NSRunningApplication.runningApplicationWithProcessIdentifier("
                                         + str(process.pid) + "); "
                                         "JSON.stringify({name:ObjC.unwrap(a.localizedName),"
                                         "id:ObjC.unwrap(a.bundleIdentifier)})")
                                identity = json.loads(subprocess.run(
                                    ["osascript", "-l", "JavaScript", "-e", query],
                                    check=True, capture_output=True, text=True, timeout=5,
                                ).stdout)
                                assert identity == {"name": "T3 Code", "id": "com.t3tools.t3code"}, identity
                                children = subprocess.run(
                                    ["ps", "-axo", "pid=,ppid=,comm="],
                                    check=True, capture_output=True, text=True,
                                ).stdout.splitlines()
                                renderer = any(
                                    len(fields := line.strip().split(None, 2)) == 3
                                    and fields[1] == str(process.pid)
                                    and "T3 Code Helper (Renderer).app" in fields[2]
                                    for line in children
                                )
                                if renderer:
                                    return {"version": version, "desktopReady": True,
                                            "applicationName": identity["name"],
                                            "nativeProcessMatchesBundle": True,
                                            "rendererRunning": True, "mainWindowCreated": True,
                                            "inheritedNodeModeCleared": True, "httpStatus": 200}
                    except OSError:
                        pass
                time.sleep(0.1)
            raise RuntimeError("Desktop did not become ready in its isolated T3 home")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if runtime is not None:
                try:
                    os.kill(runtime["pid"], 15)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.platform != "darwin":
        raise SystemExit("Run in a macOS GUI session: python3 checks/t3code-desktop.py PACKAGE")
    print(json.dumps(check(Path(sys.argv[1]))))
