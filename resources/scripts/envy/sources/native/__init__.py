"""Minimal runtime probes shared with activation (standard library only)."""
import shutil
from pathlib import Path


def os_release(root=Path("/")):
    try:
        return {key: value.strip().strip('"') for line in (root / "etc/os-release").read_text().splitlines()
                if "=" in line for key, value in [line.split("=", 1)]}
    except OSError:
        return {}


def native_manager():
    return next((name for name, executable in (("apt", "apt-cache"), ("dnf", "dnf"),
                 ("pacman", "pacman"), ("zypper", "zypper")) if shutil.which(executable)), None)
