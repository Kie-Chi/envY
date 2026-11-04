"""VS Code custom checkers.

These are invoked via the checker registry when an AppSpec declares
checkers=["vscode_sync", "vscode_extensions"].
"""

from envy.config.machine_nix import read_machine_nix
from envy.doctor.model import (
    SECTION_AUTH,
    SECTION_CONFIG,
    SECTION_INSTALL,
    SECTION_SYNC,
    CheckResult,
    info,
    ok,
    warn,
)
from envy.doctor.probes import vscode as vscode_probe
from envy.schemas.apps import AppSpec, EXPECTED_EXTENSIONS


def check_sync(spec: AppSpec) -> list[CheckResult]:
    """Check VS Code mode, Settings Sync, auth, and Copilot state."""
    mode = read_machine_nix().get("envy.vscode.mode", "remote")
    results: list[CheckResult] = [info(SECTION_CONFIG, f"{spec.name} mode", f"mode={mode}")]

    if mode == "remote":
        results.extend(_check_remote_sync(spec))
    elif mode == "local":
        results.append(ok(
            SECTION_CONFIG,
            f"{spec.name} local config",
            "settings/keybindings/snippets are managed by Home Manager",
        ))
    else:
        results.append(warn(
            SECTION_CONFIG,
            f"{spec.name} mode",
            f"unknown mode={mode}",
            hint="Run: envy config set envy.vscode.mode remote  # or local",
        ))
    return results


def check_extensions(spec: AppSpec) -> list[CheckResult]:
    """Check VS Code extensions (only meaningful in local mode)."""
    mode = read_machine_nix().get("envy.vscode.mode", "remote")
    if mode != "local":
        return []  # remote mode manages extensions via Settings Sync
    return _check_local_extensions(spec)


# ==========================================
# INTERNAL HELPERS
# ==========================================


def _check_remote_sync(spec: AppSpec) -> list[CheckResult]:
    results: list[CheckResult] = []
    keys = vscode_probe.state_keys()

    if not vscode_probe.state_db_exists():
        results.append(warn(
            SECTION_SYNC,
            f"{spec.name} settings sync",
            "VS Code state database is missing",
            hint="Open VS Code, sign in, and enable Settings Sync.",
        ))
        return results

    if {"sync.enable", "userDataSyncAccountProvider"} <= keys:
        results.append(ok(SECTION_SYNC, f"{spec.name} settings sync", "sync state keys are present"))
    else:
        results.append(warn(
            SECTION_SYNC,
            f"{spec.name} settings sync",
            "Settings Sync does not appear to be enabled",
            hint="In VS Code: Accounts menu -> Turn on Settings Sync.",
        ))

    has_auth = any(
        key.startswith('secret://{"extensionId":"vscode.github-authentication"')
        or key == "vscode.github-authentication"
        or key.startswith("github-")
        for key in keys
    )
    if has_auth:
        results.append(ok(SECTION_AUTH, f"{spec.name} account", "GitHub/Microsoft auth state is present"))
    else:
        results.append(warn(
            SECTION_AUTH,
            f"{spec.name} account",
            "no VS Code account auth marker found",
            hint="Use the Accounts menu in VS Code to sign in.",
        ))

    has_copilot = any(key.startswith("GitHub.copilot") or "copilot" in key.lower() for key in keys)
    if has_copilot:
        results.append(ok(SECTION_AUTH, f"{spec.name} copilot", "Copilot state marker is present"))
    else:
        results.append(info(
            SECTION_AUTH,
            f"{spec.name} copilot",
            "Copilot state marker not found",
            hint="Sign in to GitHub Copilot if this machine should use it.",
        ))

    return results


def _check_local_extensions(spec: AppSpec) -> list[CheckResult]:
    results: list[CheckResult] = [
        ok(SECTION_INSTALL, f"{spec.name} local extensions", f"{len(EXPECTED_EXTENSIONS)} extensions are declared in Nix"),
    ]

    command = vscode_probe.code_command()
    if not command:
        results.append(warn(
            SECTION_INSTALL,
            f"{spec.name} code cli",
            "code command not found",
            hint="Open VS Code and install the shell command, or rerun after Home Manager activation.",
        ))
        return results

    installed = vscode_probe.installed_extensions(command)
    if not installed:
        results.append(info(
            SECTION_INSTALL,
            f"{spec.name} extensions installed",
            "could not read installed extensions yet",
            hint="Rerun after VS Code has been opened once.",
        ))
        return results

    missing = sorted(set(EXPECTED_EXTENSIONS) - installed)
    if missing:
        results.append(warn(
            SECTION_INSTALL,
            f"{spec.name} extensions installed",
            f"{len(missing)} declared extensions are not visible to code CLI",
            hint="Run: envy apply; then open VS Code once. Missing: " + ", ".join(missing[:8]),
        ))
    else:
        results.append(ok(SECTION_INSTALL, f"{spec.name} extensions installed", "all declared extensions are visible"))

    return results
