"""Shared utilities for envy CLI — path constants, subprocess helpers, sudo wrapper."""

import json
import os
import re
import subprocess
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Optional

from envy import log
from envy.process import run_process
from envy.secure_io import atomic_write_text, secure_copy
from envy.sops_format import content_is_sops_encrypted

# ==========================================
# PLATFORM
# ==========================================

PLATFORM = sys.platform  # "linux" | "darwin"


def platform_name() -> str:
    """Return the repository platform namespace for this machine."""
    return "darwin" if PLATFORM == "darwin" else "linux"

# ==========================================
# PATHS
# ==========================================

HOME_DIR = Path.home()
LEGACY_MACHINE_SELECTOR = HOME_DIR / ".config" / "envy" / "machine"
LEGACY_USER_CONFIG = HOME_DIR / ".config" / "dotfiles" / "config.nix"
LEGACY_SYSTEM_CONFIG = Path("/etc/dotfiles/config.nix")


def _is_envy_checkout(path: Path) -> bool:
    return (path / "flake.nix").is_file() and (path / "resources/scripts/envy").is_dir()


def _resolve_envy_root() -> Path:
    for name in ("ENVY_ROOT", "ENVY_DOTFILES", "DOTFILES_DIR"):
        value = os.environ.get(name)
        if value:
            candidate = Path(value).expanduser()
            if _is_envy_checkout(candidate):
                return candidate
    working_checkout = Path.cwd()
    if _is_envy_checkout(working_checkout):
        return working_checkout
    source_checkout = Path(__file__).resolve().parents[3]
    if _is_envy_checkout(source_checkout):
        return source_checkout
    return HOME_DIR / ".envy"


ENVY_ROOT = _resolve_envy_root()
HOSTS_DIR = ENVY_ROOT / "hosts"

# Platform-specific age key directory:
#   Linux: ~/.config/sops/age
#   macOS: ~/Library/Application Support/sops/age
AGE_KEY_DIR = (
    HOME_DIR / "Library" / "Application Support" / "sops" / "age"
    if PLATFORM == "darwin"
    else HOME_DIR / ".config" / "sops" / "age"
)
AGE_KEY_FILE = AGE_KEY_DIR / "keys.txt"


def age_key_file_for_home(home: Path) -> Path:
    """Return the age identity path used by sops-nix for a configured home."""
    return (
        home / "Library" / "Application Support" / "sops" / "age" / "keys.txt"
        if PLATFORM == "darwin"
        else home / ".config" / "sops" / "age" / "keys.txt"
    )
SOPS_YAML = ENVY_ROOT / ".sops.yaml"
SECRETS_DIR = ENVY_ROOT / "secrets"
SECRETS_FILE = SECRETS_DIR / "secrets.yaml"
RECOVERY_KEY_FILE = SECRETS_DIR / "recovery-key.age"
DEVICE_LABEL_FILE = ENVY_ROOT / ".device-label"
SETUP_SCRIPT = ENVY_ROOT / "setup.sh"


def state_dir() -> Path:
    """Return the persistent per-user state directory for envy.

    Distinct from the ~/.cache convention used for manifests and the registry
    index: state written here (e.g. the operation journal) must survive cache
    clears and `envy clean`, which only runs nix-collect-garbage/brew cleanup.
    """
    root = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(root).expanduser() if root else HOME_DIR / ".local" / "state"
    return base / "envy"

# Darwin system profile. The flake target is resolved at runtime because every
# machine has its own hosts/<platform>/<id>.nix entry.
SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")


def read_device_metadata() -> dict[str, str]:
    """Read .device-label TOML, accepting the previous one-line label once."""
    if not DEVICE_LABEL_FILE.exists():
        return {}
    text = DEVICE_LABEL_FILE.read_text().strip()
    if not text:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", text):
            # Historically this file named only the sops key. Do not assume it
            # was also the selected machine when legacy config may disagree.
            return {"sops_label": text}
        raise ValueError(f"invalid TOML in {DEVICE_LABEL_FILE}: {exc}") from exc

    version = data.get("version", 1)
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported device metadata version: {version}")
    device = data.get("device", {})
    if not isinstance(device, dict):
        raise ValueError(f"[device] must be a TOML table in {DEVICE_LABEL_FILE}")
    result = {}
    for key in ("machine_id", "sops_label"):
        value = device.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"device.{key} must be a string in {DEVICE_LABEL_FILE}")
            result[key] = value.strip()
    return result


def device_metadata_is_toml() -> bool:
    if not DEVICE_LABEL_FILE.exists():
        return False
    try:
        data = tomllib.loads(DEVICE_LABEL_FILE.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return (
        type(data.get("version")) is int
        and data.get("version") == 1
        and isinstance(data.get("device"), dict)
    )


def write_device_metadata(
    *, machine_id: str | None = None, sops_label: str | None = None
) -> None:
    """Update device-local TOML while preserving the other identity field."""
    current = read_device_metadata()
    if machine_id is not None:
        current["machine_id"] = machine_id
    if sops_label is not None:
        current["sops_label"] = sops_label
    DEVICE_LABEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version = 1\n", "\n", "[device]\n"]
    for key in ("machine_id", "sops_label"):
        value = current.get(key)
        if value:
            lines.append(f"{key} = {json.dumps(value)}\n")
    atomic_write_text(DEVICE_LABEL_FILE, "".join(lines), mode=0o600)


def current_machine_id() -> str:
    """Resolve the machine target from device metadata and legacy inputs."""
    env_machine = os.environ.get("ENVY_MACHINE", "").strip()
    candidates = [env_machine]
    metadata = read_device_metadata()
    candidates.append(metadata.get("machine_id", ""))

    if not metadata.get("machine_id"):
        # One-time upgrade paths are consulted only until TOML device metadata
        # has a machine ID; they are not part of the steady-state lookup.
        if LEGACY_MACHINE_SELECTOR.exists():
            try:
                candidates.append(LEGACY_MACHINE_SELECTOR.read_text().strip())
            except OSError:
                pass

        for config_path in (LEGACY_USER_CONFIG, ENVY_ROOT / "config.nix", LEGACY_SYSTEM_CONFIG):
            if not config_path.exists():
                continue
            try:
                text = config_path.read_text()
            except OSError:
                continue
            match = re.search(r'^\s*envy\.machine\.id\s*=\s*"([A-Za-z0-9_-]+)"\s*;', text, re.MULTILINE)
            if match:
                candidates.append(match.group(1))

    # A sops label identifies an encryption recipient, not a machine target.
    # Never let a copied key or stale label select another host configuration.
    hostname = subprocess.getoutput("hostname -s").strip() or "machine"
    candidates.append(re.sub(r"[^A-Za-z0-9_-]", "_", hostname))
    for candidate in candidates:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", candidate or ""):
            return candidate
    return "machine"


def set_device_machine_id(machine_id: str) -> None:
    """Persist the machine target in device metadata; policy stays in Git."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", machine_id):
        raise ValueError("invalid machine ID")
    write_device_metadata(machine_id=machine_id)
    if LEGACY_MACHINE_SELECTOR.is_file() or LEGACY_MACHINE_SELECTOR.is_symlink():
        LEGACY_MACHINE_SELECTOR.unlink()
        try:
            LEGACY_MACHINE_SELECTOR.parent.rmdir()
        except OSError:
            pass


def flake_target() -> str:
    # Both nix-darwin and Home Manager select the local machine by flake name.
    return f"path:.#{current_machine_id()}"


def machine_config_dir(platform: str | None = None) -> Path:
    return HOSTS_DIR / (platform or platform_name())


def machine_config_file(machine_id: str | None = None, platform: str | None = None) -> Path:
    return machine_config_dir(platform) / f"{machine_id or current_machine_id()}.nix"


def machine_manifest_attr(machine_id: str | None = None) -> str:
    selected = machine_id or current_machine_id()
    if platform_name() == "darwin":
        return f"path:.#darwinConfigurations.{selected}.config.envy.machine.manifest"
    return f"path:.#homeConfigurations.{selected}.config.envy.machine.manifest"


def machine_build_attr(machine_id: str | None = None, *, drv_path: bool = False) -> str:
    selected = machine_id or current_machine_id()
    if platform_name() == "darwin":
        attr = f"path:.#darwinConfigurations.{selected}.config.system.build.toplevel"
    else:
        attr = f"path:.#homeConfigurations.{selected}.activationPackage"
    return f"{attr}.drvPath" if drv_path else attr


# ==========================================
# SUBPROCESS
# ==========================================


def run_cmd(
    cmd: list,
    stdin_data: str | None = None,
    check: bool = True,
    cwd: Path | None = None,
    capture: bool = True,
    activity: str | None = None,
    age_key_file: Path | None = None,
) -> str | None:
    """Run subprocess command. capture=True returns stdout as string; capture=False streams live."""
    env = os.environ.copy()
    env.pop("SOPS_AGE_KEY", None)
    env.pop("SOPS_AGE_KEY_FILE", None)
    env.pop("SOPS_AGE_SSH_PRIVATE_KEY_FILE", None)
    selected_age_key = age_key_file or AGE_KEY_FILE
    # Always pin sops to the selected identity path. When the file is absent,
    # sops must fail instead of falling back to an ambient SSH identity or a
    # different default age file.
    env["SOPS_AGE_KEY_FILE"] = str(selected_age_key)

    effective_cwd = str(cwd or ENVY_ROOT)

    result = run_process(
        cmd,
        stdin_data=stdin_data,
        capture=capture,
        check=check,
        env=env,
        cwd=effective_cwd,
        activity=activity,
    )
    return result.stdout.strip() if capture and result.stdout else ("" if capture else None)

# ==========================================
# SUDO WRAPPER
# ==========================================


def _get_sudo_passwd() -> str | None:
    """Read password from sops-encrypted secrets if available."""
    if not SECRETS_FILE.exists() or not AGE_KEY_FILE.exists():
        return None
    try:
        child_env = os.environ.copy()
        for variable in ("SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE", "SOPS_AGE_SSH_PRIVATE_KEY_FILE"):
            child_env.pop(variable, None)
        child_env["SOPS_AGE_KEY_FILE"] = str(AGE_KEY_FILE)
        result = subprocess.run(
            ["sops", "-d", "--extract", '["home"]["passwd"]', str(SECRETS_FILE)],
            capture_output=True, text=True,
            env=child_env,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _sudo_ticket_available() -> bool:
    """Return whether sudo can authenticate without prompting right now.

    The TUI runs streamed workflows with stdin closed.  A cached sudo
    timestamp is therefore just as useful as the password stored in sops, but
    checking only the latter would unnecessarily send otherwise non-interactive
    applies back to the outer terminal.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def is_sops_encrypted(path: Path) -> bool:
    """Check sops metadata and ensure no non-empty plaintext data leaves exist."""
    if not path.exists():
        return False
    try:
        return content_is_sops_encrypted(path.read_text())
    except (OSError, UnicodeDecodeError):
        return False


def backup_sensitive_file(filepath: Path) -> Optional[Path]:
    """Create a .bak copy of a sensitive file before overwriting it.
    Returns the backup path, or None if the source doesn't exist."""
    if not filepath.exists():
        return None
    bak = filepath.with_suffix(filepath.suffix + ".bak")
    try:
        secure_copy(filepath, bak)
        return bak
    except OSError:
        return None


def esudo(
    *args: str, capture: bool = False, cwd: Path | None = None,
    activity: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """sudo with automatic password injection from sops.
    capture=False: stream output live (default, for long-running commands).
    capture=True:  capture output for return-value inspection."""
    effective_cwd = str(cwd or ENVY_ROOT)
    passwd = _get_sudo_passwd()
    if passwd:
        # Try password-based sudo first
        result = run_process(
            ["sudo", "-S", *args],
            stdin_data=passwd,
            capture=capture,
            check=False,
            cwd=effective_cwd,
            activity=activity,
        )
        if result.returncode == 0:
            if capture and result.stdout:
                print(result.stdout)
            return result
        # Fall through to interactive sudo
    # A cached sudo ticket can authenticate without a prompt even when the TUI
    # has closed stdin for a streamed workflow. Reuse it explicitly before
    # deciding that this apply needs the outer interactive terminal.
    if not sys.stdin.isatty():
        if _sudo_ticket_available():
            return run_process(
                ["sudo", "-n", *args], cwd=effective_cwd, capture=capture,
                check=True, activity=activity,
            )
        # Interactive sudo needs a TTY to prompt for the password. When stdin
        # is not a terminal, refuse instead of hanging on an invisible prompt.
        log.error(
            "sudo",
            "interactive sudo required but no TTY is available",
            hint="run this in a terminal, or provision the sops sudo password",
        )
        raise SystemExit(1)
    return run_process(
        ["sudo", *args], cwd=effective_cwd, capture=capture, check=True,
        activity=activity,
    )

# ==========================================
# APPLY ROUTING
# ==========================================


def run_hm(*args: str) -> None:
    """Run home-manager or fallback to nix run. Output streams live to terminal."""
    if _command_exists("home-manager"):
        run_cmd(["home-manager", *args], capture=False, activity="Home Manager activation")
    else:
        log.warn("hm", "'home-manager' not found, using repository-locked fallback")
        run_cmd([
            "nix", "run", "--inputs-from", "path:.", "home-manager", "--", *args,
        ], capture=False, activity="Home Manager activation")


def run_darwin_switch() -> None:
    """Run nix-darwin switch with sudo."""
    etc_migration = ENVY_ROOT / "resources" / "scripts" / "nix-darwin-etc.sh"
    if not etc_migration.is_file():
        raise RuntimeError(f"missing nix-darwin /etc migration script: {etc_migration}")
    log.step("darwin", "preparing /etc for nix-darwin")
    esudo(
        "--preserve-env=ENVY_NIX_DARWIN_ETC_ROOT", str(etc_migration),
        capture=False, activity="preparing nix-darwin /etc files",
    )

    log.step("darwin", "running nix-darwin switch")
    runner = shutil.which("darwin-rebuild")
    if runner:
        log.info("darwin", "using installed runner", path=runner)
        command = [runner, "switch", "--flake", flake_target(), "--impure"]
    else:
        log.warn("darwin", "darwin-rebuild not found, using repository-locked fallback")
        command = [
            # sudo does not retain bootstrap's NIX_CONFIG or the user's Nix
            # configuration, so enable flake support on this invocation too.
            "nix", "--extra-experimental-features", "nix-command flakes",
            "run", "--inputs-from", "path:.", "darwin", "--", "switch",
            "--flake", flake_target(), "--impure",
        ]
    esudo(
        "--preserve-env=HOME,NIX_CONFIG", *command, capture=False,
        activity="nix-darwin build and activation",
    )
    log.ok("darwin", "system successfully updated")


def run_apply() -> None:
    """Apply configuration — routes to platform-appropriate method."""
    if PLATFORM == "darwin":
        run_darwin_switch()
    else:
        log.step("hm", "applying Home Manager configuration")
        run_hm("switch", "--flake", flake_target(), "--impure")
        log.ok("hm", "configuration applied, new generation created")


def _command_exists(name: str) -> bool:
    return subprocess.run(
        ["which", name], capture_output=True,
    ).returncode == 0
