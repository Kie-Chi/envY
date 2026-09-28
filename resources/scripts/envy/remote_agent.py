"""Launch an agent on another machine and give it an SSH path back here.

The command deliberately keeps connection and provider policy in a user-owned TOML
file.  It never copies a private key: only the selected remote public key is added
to this machine's authorized_keys file.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import typer


app = typer.Typer(
    name="remote-agent",
    help="Launch a remote agent that must SSH back to this machine to do the work.",
    no_args_is_help=True,
)

DEFAULT_CONFIG = Path.home() / ".config" / "envy" / "remote-agents.toml"
LEGACY_CONFIG = Path.home() / ".config" / "envy" / "agents.toml"
DEFAULT_REMOTE_IDENTITY = "~/.ssh/id_ed25519"
DEFAULT_AUTHORIZED_KEYS = Path.home() / ".ssh" / "authorized_keys"
MANDATORY_SYSTEM_PROMPT = (
    "REMOTE AGENT OPERATING CONTRACT\n"
    "==============================\n"
    "You are running the agent process on the remote runtime host: {ssh_target}.\n"
    "The actual task workspace and execution target is the launcher host: {user}@{host}:{port}.\n"
    "This envY connection profile is: {connection}.\n"
    "The selected provider is: {agent}.\n"
    "The provider command is: {agent_command}.\n"
    "The provider configuration is: {agent_config}.\n\n"
    "MANDATORY PROCEDURE\n"
    "1. You MUST first connect back to the launcher host before reading, changing, or analyzing task files.\n"
    "2. Use this return command template, replacing {{remote_command}} with the command to execute:\n"
    "   {return_command}\n"
    "3. You MUST perform all actual work on {host} as {user}; do not complete the task only on {ssh_target}.\n"
    "4. If the return connection fails, you MUST stop and report the connection failure. Do not fall back to\n"
    "   operating on the remote runtime host.\n"
    "The remote runtime host exists only to run this agent and provide the return connection."
)
REMOTE_KEY_SCRIPT = r'''set -eu
p=$1
case "$p" in
  "~/"*) p="$HOME/${p#~/}" ;;
  "\$HOME/"*) p="$HOME/${p#\$HOME/}" ;;
esac
for candidate in "$p.pub" "$p"; do
  [ -f "$candidate" ] || continue
  case "$candidate" in
    *.pub) cat "$candidate" ;;
    *) ssh-keygen -y -f "$candidate" ;;
  esac
  exit 0
done
printf '%s\n' "No usable public key found for configured identity: $p" >&2
exit 1
'''


class AgentConfigError(ValueError):
    """A user-editable agent configuration is invalid."""


@dataclass(frozen=True)
class Connection:
    name: str
    method: str
    host: str | None
    args: tuple[str, ...]
    command: tuple[str, ...]
    alias: str | None
    shell: str
    remote_identity: str
    return_host: str
    return_user: str
    return_port: int
    authorized_keys: Path
    return_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpec:
    name: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    prompt_args: tuple[str, ...]
    config: str | None
    config_args: tuple[str, ...]
    env: dict[str, str]


def _fail(message: str, *, code: int = 2) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=code)


def _string(value: Any, field: str, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise AgentConfigError(f"{field} must be a non-empty string")
    return value


def _list(value: Any, field: str, *, default: Iterable[str] = ()) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentConfigError(f"{field} must be a string or a list of strings")
    return tuple(value)


def _table(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentConfigError(f"{field} must be a TOML table")
    return value


def _read_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError:
        raise AgentConfigError(
            f"configuration not found: {path}; run `envy agent init` and edit it"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise AgentConfigError(f"invalid TOML in {path}: {exc}") from None
    if not isinstance(value, dict):
        raise AgentConfigError(f"configuration root must be a TOML table: {path}")
    return value


def _config_path(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser()
    configured = os.environ.get("ENVY_REMOTE_AGENT_CONFIG") or os.environ.get("ENVY_AGENT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    # Keep an older agents.toml usable, but new init commands are explicit about
    # the remote-agent role and write remote-agents.toml.
    if not DEFAULT_CONFIG.exists() and LEGACY_CONFIG.exists():
        return LEGACY_CONFIG
    return DEFAULT_CONFIG


def _resolve_connection(
    root: dict[str, Any], name: str, *, local_user: str, local_host: str
) -> Connection:
    tables = root.get("connections", root.get("targets", {}))
    tables = _table(tables, "connections")
    raw = _table(tables.get(name), f"connections.{name}")
    method = str(raw.get("method", "ssh")).lower()
    if method not in {"ssh", "command", "alias"}:
        raise AgentConfigError(
            f"connections.{name}.method must be ssh, command, or alias"
        )

    host = _string(raw.get("host"), f"connections.{name}.host")
    command = _list(raw.get("command"), f"connections.{name}.command")
    alias = _string(raw.get("alias"), f"connections.{name}.alias")
    shell = str(raw.get("shell", os.environ.get("SHELL", "zsh")))
    args = _list(raw.get("args", raw.get("ssh_args")), f"connections.{name}.args")

    if method == "ssh" and not host:
        raise AgentConfigError(f"connections.{name}.host is required for method=ssh")
    if method == "command" and not command:
        raise AgentConfigError(
            f"connections.{name}.command is required for method=command"
        )
    if method == "alias" and not alias:
        raise AgentConfigError(f"connections.{name}.alias is required for method=alias")

    return_host = _string(raw.get("return_host"), f"connections.{name}.return_host")
    if not return_host:
        raise AgentConfigError(
            f"connections.{name}.return_host is required: the remote agent must have a reachable SSH path back"
        )
    return_user = _string(raw.get("return_user", local_user), f"connections.{name}.return_user") or local_user
    try:
        return_port = int(raw.get("return_port", 22))
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(f"connections.{name}.return_port must be an integer") from exc
    if not 1 <= return_port <= 65535:
        raise AgentConfigError(f"connections.{name}.return_port must be between 1 and 65535")

    authorized = Path(
        os.path.expandvars(os.path.expanduser(str(raw.get("authorized_keys", DEFAULT_AUTHORIZED_KEYS))))
    )
    remote_identity = str(raw.get("remote_identity", DEFAULT_REMOTE_IDENTITY))
    return_command = _list(raw.get("return_command"), f"connections.{name}.return_command")
    return Connection(
        name=name,
        method=method,
        host=host,
        args=args,
        command=command,
        alias=alias,
        shell=shell,
        remote_identity=remote_identity,
        return_host=return_host,
        return_user=return_user,
        return_port=return_port,
        authorized_keys=authorized,
        return_command=return_command,
    )


def _resolve_agent(root: dict[str, Any], name: str) -> AgentSpec:
    agents = _table(root.get("agents"), "agents")
    raw = _table(agents.get(name), f"agents.{name}")
    command = _list(raw.get("command"), f"agents.{name}.command")
    if not command:
        raise AgentConfigError(f"agents.{name}.command is required")
    args = _list(raw.get("args"), f"agents.{name}.args")
    prompt_args = _list(
        raw.get("prompt_args", ["--system-prompt", "{system_prompt}"]),
        f"agents.{name}.prompt_args",
    )
    config = _string(raw.get("config"), f"agents.{name}.config")
    if config is not None:
        config_args = _list(
            raw.get("config_args", ["--config", "{config}"]),
            f"agents.{name}.config_args",
        )
    else:
        config_args = ()
    env_raw = _table(raw.get("env"), f"agents.{name}.env")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise AgentConfigError(f"agents.{name}.env contains an invalid variable name: {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise AgentConfigError(f"agents.{name}.env.{key} must be scalar")
        env[key] = str(value)
    return AgentSpec(
        name=name,
        command=command,
        args=args,
        prompt_args=prompt_args,
        config=config,
        config_args=config_args,
        env=env,
    )


def _defaults(root: dict[str, Any]) -> dict[str, Any]:
    return _table(root.get("defaults"), "defaults")


def _names(root: dict[str, Any], section: str) -> list[str]:
    return sorted(_table(root.get(section), section))


def _format(value: str, values: dict[str, str], field: str) -> str:
    try:
        return value.format_map(values)
    except KeyError as exc:
        raise AgentConfigError(
            f"{field} uses unknown placeholder {{{exc.args[0]}}}; "
            "available placeholders are {system_prompt}, {host}, {user}, {port}, "
            "{ssh_target}, {launcher_host}, {return_command}, {agent}, {agent_command}, "
            "{agent_config}, {config}, and {connection}"
        ) from None


def _format_many(values: Iterable[str], substitutions: dict[str, str], field: str) -> list[str]:
    return [_format(value, substitutions, field) for value in values]


def _connection_target(connection: Connection) -> str:
    if connection.method == "ssh":
        assert connection.host is not None
        return connection.host
    if connection.method == "command":
        return connection.host or connection.name
    assert connection.alias is not None
    return connection.alias


def _remote_command_argv(
    connection: Connection, remote_command: str | None, *, interactive: bool
) -> list[str]:
    if connection.method == "ssh":
        ssh = shutil.which("ssh") or "ssh"
        args = list(connection.args)
        if interactive and not any(
            arg in {"-t", "-tt", "-T"} or arg.startswith("-oRequestTTY=")
            or arg.startswith("RequestTTY=")
            for arg in args
        ):
            args.insert(0, "-tt")
        result = [ssh, *args, _connection_target(connection)]
        if remote_command is not None:
            result.append(remote_command)
        return result

    if connection.method == "command":
        result = [*connection.command, *connection.args]
        if remote_command is not None:
            replaced = False
            for index, value in enumerate(result):
                if "{remote_command}" in value:
                    result[index] = value.replace("{remote_command}", remote_command)
                    replaced = True
            if not replaced:
                result.append(remote_command)
        return result

    # A configured shell alias is expanded by an interactive shell. The final
    # positional argument is the remote command, so aliases may still carry
    # their own host, port, and SSH options.
    alias = shlex.quote(connection.alias or "")
    result = [connection.shell, "-lic", f"exec {alias} \"$@\"", "--"]
    result.extend(connection.args)
    if remote_command is not None:
        result.append(remote_command)
    return result


def _run_remote(
    connection: Connection,
    remote_command: str,
    *,
    interactive: bool,
    timeout: float | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = _remote_command_argv(connection, remote_command, interactive=interactive)
    try:
        return subprocess.run(
            argv,
            check=False,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AgentConfigError(f"connection command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentConfigError(f"timed out while connecting to {_connection_target(connection)}") from exc


def _remote_public_key(connection: Connection) -> str:
    command = "sh -c %s sh %s" % (
        shlex.quote(REMOTE_KEY_SCRIPT),
        shlex.quote(connection.remote_identity),
    )
    result = _run_remote(connection, command, interactive=False, timeout=20, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AgentConfigError(
            f"could not read a public key from {_connection_target(connection)}"
            + (f": {detail}" if detail else "")
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("ssh-") or len(parts) >= 2 and parts[0] in {"ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"}:
            return " ".join(parts[:3])
    raise AgentConfigError("remote key command did not return an OpenSSH public key")


def _key_identity(public_key: str) -> tuple[str, str]:
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise AgentConfigError("remote public key is malformed")
    return parts[0], parts[1]


def _install_authorized_key(path: Path, public_key: str, comment: str, *, dry_run: bool) -> bool:
    key_type, key_blob = _key_identity(public_key)
    path = path.expanduser()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in existing.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key_type and fields[1] == key_blob:
            return False
    line = f"{key_type} {key_blob} {comment}\n"
    if dry_run:
        return True
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(line)
    os.chmod(path, 0o600)
    return True


def _return_command_parts(connection: Connection, remote_command: str) -> list[str]:
    values = {
        "host": connection.return_host,
        "user": connection.return_user,
        "port": str(connection.return_port),
        "remote_command": remote_command,
        "connection": connection.name,
    }
    if connection.return_command:
        parts = _format_many(
            connection.return_command,
            values,
            "connections.return_command",
        )
        if not any("{remote_command}" in value for value in connection.return_command):
            parts.append(remote_command)
        return parts
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-i", _remote_path(connection.remote_identity),
        "-p", str(connection.return_port),
        f"{connection.return_user}@{connection.return_host}",
        remote_command,
    ]


def _return_command_display(connection: Connection, remote_command: str = "true") -> str:
    return " ".join(_remote_shell_arg(value) for value in _return_command_parts(connection, remote_command))


def _return_command_template(connection: Connection) -> str:
    return _return_command_display(connection, "{remote_command}")


def _system_prompt(
    defaults: dict[str, Any],
    connection: Connection,
    agent_name: str,
    spec: AgentSpec,
    override: str | None,
    launcher_host: str,
) -> str:
    values = {
        "host": connection.return_host,
        "user": connection.return_user,
        "ssh_target": _connection_target(connection),
        "launcher_host": launcher_host,
        "connection": connection.name,
        "port": str(connection.return_port),
        "return_command": _return_command_template(connection),
        "agent": agent_name,
        "agent_command": shlex.join(spec.command),
        "agent_config": spec.config or "(provider default)",
        "system_prompt": "",
        "config": "",
    }
    mandatory = _format(MANDATORY_SYSTEM_PROMPT, values, "system_prompt")
    configured = override or defaults.get("system_prompt")
    if not configured:
        return mandatory
    additional = _format(str(configured), values, "system_prompt")
    return mandatory + "\n\nAdditional instructions from configuration:\n" + additional


def _agent_argv(
    spec: AgentSpec,
    prompt: str,
    connection: Connection,
    launcher_host: str,
    extra_args: list[str],
) -> list[str]:
    config = _format(spec.config, {"system_prompt": prompt, "host": connection.return_host, "user": connection.return_user, "ssh_target": _connection_target(connection), "launcher_host": launcher_host, "connection": connection.name, "config": spec.config or ""}, "agents.config") if spec.config else ""
    if config:
        config = _remote_path(config)
    values = {
        "system_prompt": prompt,
        "host": connection.return_host,
        "user": connection.return_user,
        "ssh_target": _connection_target(connection),
        "launcher_host": launcher_host,
        "connection": connection.name,
        "port": str(connection.return_port),
        "config": config,
    }
    result = _format_many(spec.command, values, "agents.command")
    result.extend(_format_many(spec.args, values, "agents.args"))
    result.extend(_format_many(spec.config_args, values, "agents.config_args"))
    result.extend(_format_many(spec.prompt_args, values, "agents.prompt_args"))
    result.extend(extra_args)
    return result


def _remote_path(value: str) -> str:
    """Render a user path for the remote shell without expanding it locally."""
    if value == "~":
        return "$HOME"
    if value.startswith("~/"):
        return "$HOME/" + value[2:]
    if value == "$HOME" or value.startswith("$HOME/"):
        return value
    return value


def _remote_shell_arg(value: str) -> str:
    # Keep $HOME as a shell expression for paths originating from the remote
    # machine. All other values are quoted as literal shell arguments.
    if value == "$HOME" or value.startswith("$HOME/"):
        suffix = value[len("$HOME"):]
        safe_suffix = suffix.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
        return f'"$HOME{safe_suffix}"'
    return shlex.quote(value)


def _return_path_probe(connection: Connection) -> str:
    return _return_command_display(connection)


def _verify_return_path(connection: Connection) -> None:
    result = _run_remote(
        connection,
        _return_path_probe(connection),
        interactive=False,
        timeout=20,
        capture=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AgentConfigError(
            f"remote agent cannot connect back to {connection.return_user}@{connection.return_host}:{connection.return_port}"
            + (f": {detail}" if detail else "")
        )


def _remote_agent_command(spec: AgentSpec, command: list[str], connection: Connection, workdir: str | None) -> str:
    substitutions = {
        "host": connection.return_host,
        "user": connection.return_user,
        "ssh_target": _connection_target(connection),
        "launcher_host": socket.gethostname(),
        "connection": connection.name,
        "port": str(connection.return_port),
        "return_command": _return_command_template(connection),
        "agent": spec.name,
        "agent_command": shlex.join(spec.command),
        "agent_config": spec.config or "(provider default)",
        "system_prompt": "",
        "config": _remote_path(spec.config or ""),
    }
    env = {
        "ENVY_REMOTE_AGENT": "1",
        "ENVY_REMOTE_RETURN_HOST": connection.return_host,
        "ENVY_REMOTE_RETURN_USER": connection.return_user,
        "ENVY_REMOTE_RETURN_PORT": str(connection.return_port),
        "ENVY_REMOTE_RETURN_COMMAND": _return_command_template(connection),
    }
    env.update({
        key: _format(value, substitutions, f"agents.env.{key}")
        for key, value in spec.env.items()
    })
    prefix = ""
    if workdir:
        prefix += f"cd {_remote_shell_arg(_remote_path(workdir))} && "
    if env:
        prefix += "env " + " ".join(f"{shlex.quote(key)}={_remote_shell_arg(_remote_path(value)) if value.startswith(('~/', '$HOME/')) else shlex.quote(value)}" for key, value in env.items()) + " "
    return prefix + " ".join(_remote_shell_arg(value) for value in command)


def _agent_names(root: dict[str, Any]) -> list[str]:
    names = _names(root, "agents")
    if not names:
        raise AgentConfigError("no agents are configured")
    return names


EXAMPLE_CONFIG = '''# User-owned configuration for `envy remote-agent start`.
# SSH method uses ~/.ssh/config, so `host` may be an SSH Host alias.

[defaults]
target = "remote-agent"
agent = "claude"
# The tool always prepends a structured return-to-launcher contract.
# Additional placeholders: {host}, {user}, {port}, {ssh_target}, {return_command},
# {agent}, {agent_command}, {agent_config}, {launcher_host}, and {connection}.
system_prompt = "After connecting back, use the launcher host's local tools and filesystem for this task."

[connections.remote-agent]
method = "ssh"
host = "remote-host-from-ssh-config"
args = ["-A"]
# The remote agent uses this key to connect back to this machine.
remote_identity = "~/.ssh/id_ed25519"
# This is the address the remote host can use to reach this machine.
return_host = "this-machine-reachable-name"
return_user = "your-user"
return_port = 22
# Optional wrapper for the remote-to-launcher connection. It replaces standard ssh.
# return_command = ["awesun", "ssh", "{user}@{host}:{port}", "--", "{remote_command}"]
# Optional; defaults to ~/.ssh/authorized_keys.
# authorized_keys = "~/.ssh/authorized_keys"

[agents.claude]
command = ["claude"]
args = ["--dangerously-skip-permissions"]
prompt_args = ["--system-prompt", "{system_prompt}"]

[agents.codex]
command = ["codex"]
# Codex installations may expose developer instructions through config; adapt this to the installed version.
args = []
prompt_args = ["-c", "developer_instructions={system_prompt}"]

# A custom provider may use a config file under ~/.config on the remote host.
# [agents.my-agent]
# command = ["my-agent"]
# config = "~/.config/my-agent/config.toml"
# config_args = ["--config", "{config}"]
# prompt_args = ["--system-prompt", "{system_prompt}"]
'''


@app.command("init")
def init_config(
    force: bool = typer.Option(False, "--force", help="Replace an existing example configuration."),
    config_file: Path | None = typer.Option(None, "--config-file", help="Configuration file to create."),
):
    """Create a commented remote-agent configuration under ~/.config/envy."""
    path = _config_path(config_file)
    if path.exists() and not force:
        _fail(f"configuration already exists: {path} (use --force to replace it)", code=1)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    os.chmod(path, 0o600)
    typer.echo(f"created {path}")


@app.command("list")
def list_config(
    config_file: Path | None = typer.Option(None, "--config-file", help="Configuration file to inspect."),
):
    """List configured connections and agents without connecting anywhere."""
    path = _config_path(config_file)
    try:
        root = _read_config(path)
    except AgentConfigError as exc:
        _fail(str(exc), code=1)
    connections = _names(root, "connections")
    agents = _names(root, "agents")
    typer.echo(f"config: {path}")
    typer.echo("connections: " + (", ".join(connections) if connections else "(none)"))
    typer.echo("agents: " + (", ".join(agents) if agents else "(none)"))


@app.command("start", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def start_agent(
    ctx: typer.Context,
    target: str | None = typer.Argument(None, help="Configured connection name."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Configured agent name."),
    prompt: str | None = typer.Option(None, "--prompt", help="Override the configured system prompt."),
    config_file: Path | None = typer.Option(None, "--config-file", help="Configuration file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the planned command without starting the agent."),
    workdir: str | None = typer.Option(None, "--workdir", help="Working directory on the remote agent host."),
):
    """Require an SSH return path, then start the remote agent interactively."""
    path = _config_path(config_file)
    try:
        root = _read_config(path)
        defaults = _defaults(root)
        connection_name = target or str(defaults.get("target", ""))
        if not connection_name:
            raise AgentConfigError("no target supplied and defaults.target is not set")
        agent_name = agent or str(defaults.get("agent", ""))
        if not agent_name:
            raise AgentConfigError("no agent supplied and defaults.agent is not set")
        connection = _resolve_connection(
            root,
            connection_name,
            local_user=os.environ.get("USER", ""),
            local_host=socket.gethostname(),
        )
        spec = _resolve_agent(root, agent_name)
        launcher_host = socket.gethostname()
        system_prompt = _system_prompt(defaults, connection, agent_name, spec, prompt, launcher_host)
        extra_args = list(ctx.args)
        command = _agent_argv(spec, system_prompt, connection, launcher_host, extra_args)
        remote_command = _remote_agent_command(spec, command, connection, workdir)

        public_key = _remote_public_key(connection)
        key_added = _install_authorized_key(
            connection.authorized_keys,
            public_key,
            f"envy-remote-agent:{connection.name}",
            dry_run=dry_run,
        )
        if not dry_run:
            _verify_return_path(connection)
        typer.echo(
            f"connection={connection.name} target={_connection_target(connection)} "
            f"agent={agent_name} return={connection.return_user}@{connection.return_host}:{connection.return_port}"
        )
        if public_key is not None:
            typer.echo(
                ("would add" if dry_run else ("added" if key_added else "already present"))
                + f" remote public key in {connection.authorized_keys}"
            )
        if dry_run:
            typer.echo("remote command: " + shlex.join(_remote_command_argv(connection, remote_command, interactive=True)))
            return

        result = _run_remote(connection, remote_command, interactive=True)
        if result.returncode:
            raise typer.Exit(code=result.returncode)
    except AgentConfigError as exc:
        _fail(str(exc), code=1)
    except OSError as exc:
        _fail(str(exc), code=1)


# Keep the module directly executable for simple debugging in a checkout.
if __name__ == "__main__":
    app()
