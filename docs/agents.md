# Agents module

`modules/agents` owns command-line/desktop agent installation, provider-specific wrappers, and reusable skills. Authentication files, history, caches, sessions, and plugin runtime state remain application-owned.

## Layout

```text
modules/agents/
├── default.nix          # shared provider defaults and skill selection
├── darwin.nix           # Darwin application distribution for nix-darwin
├── linux.nix            # Linux CLI/IDE distribution and runtime plugins
├── linux/rtk.nix        # Linux-only RTK package
├── claude.nix           # Claude package, wrapper defaults, and ccli alias
├── skills.nix           # skill catalog/selection Home Manager module
└── skills/
    ├── catalog.nix      # skill registry and target agents
    └── <skill-name>/
        ├── SKILL.md
        ├── agents/openai.yaml  # recommended for Codex UI metadata
        ├── scripts/            # optional deterministic helpers
        ├── references/         # optional, read only when needed
        └── assets/             # optional output resources
```

## Skill loading model

There are two distinct selection stages:

1. `agents.skills.active` controls which catalog entries are linked into `~/.codex/skills` and/or `~/.claude/skills` during Home Manager activation.
2. For a discovered skill, the agent sees its `name` and `description` metadata first. It reads the `SKILL.md` body only when the task triggers that skill, then reads referenced resources as needed.

This keeps agent capabilities declarative while preserving progressive, task-driven context loading. Changing the physically discoverable set requires a rebuild; ordinary task-level loading does not.

## Adding a skill subpackage

Create `modules/agents/skills/<skill-name>/SKILL.md`. Use lowercase letters, digits, and hyphens for the directory and frontmatter name. Keep `SKILL.md` concise; place detailed variants in one-level-deep `references/`, repeatable code in `scripts/`, and output templates in `assets/`.

Register it in `skills/catalog.nix`:

```nix
{
  envy-maintainer = {
    source = ./envy-maintainer;
    targets = [ "codex" "claude" ];
  };
}
```

Then activate it in `modules/agents/default.nix`:

```nix
agents.skills.active = [ "envy-maintainer" ];
```

Use a single shared source when both agents accept the same workflow. Split it into provider-specific skills only when frontmatter or behavior genuinely differs.

External skill repositories should be pinned as non-flake inputs in `flake.nix`,
passed to Home Manager through `extraSpecialArgs`, and registered from their
store paths in `skills/catalog.nix`. Keep provider-native packaging intact: for
example, Academic Research Skills exposes four coordinated Claude skills, while
its Codex sibling exposes one `academic-research-suite` router skill. Updating an
external skill is then an explicit `nix flake update <input-name>` operation.

## Provider boundary

Each provider gets its own module (`claude.nix`, later `codex.nix`, `gemini.nix`, and so on). A provider module may install packages, create a stable command wrapper, and link non-secret configuration. Shared LLM API keys and their shell template are owned by `modules/llm`; provider modules consume the activation-time environment and must never turn secrets into Nix evaluation-time strings.

The Claude wrapper deliberately invokes nixpkgs' `claude` wrapper, not its internal `.claude-wrapped` binary. Nixpkgs uses the outer wrapper to set required runtime environment variables and `PATH`; bypassing it would make this repository responsible for duplicating upstream packaging details.

## Shared agent configuration

The provider modules use Home Manager's `config.agents.*` option tree internally. Wrapper behavior, security-sensitive arguments, and skill discovery are shared repository settings configured in `modules/agents/default.nix`, not machine-specific values managed by `envy config`.

The two files contribute to different module systems. `default.nix` is imported
through the cross-platform `home.nix` composition root. `darwin.nix` is imported
through the repository's `darwin.nix` composition root and contributes only
Darwin Homebrew distribution policy. `flake.nix` does not import either feature
implementation directly.

The current shared configuration is intentionally explicit:

```nix
agents = {
  claude = {
    extraArgs = [ "--dangerously-skip-permissions" ];
    ccliAlias = true;
  };

  skills = {
    active = [
      "academic-research-suite"
      "academic-paper"
      "academic-paper-reviewer"
      "academic-pipeline"
      "deep-research"
    ];
  };
};
```

The Claude package selection, prompt path, and skill catalog remain Nix-only implementation settings. A machine that cannot install Claude excludes the stable package name with `envy.software.nix.packages.exclude = [ "claude" ];`; no provider-specific enable option is needed. Authentication, history, caches, sessions, and other application-owned state are not managed by this module.

CodeGraph and Headroom are shared agent tools on Darwin and Linux. The feature
contributes them through `envy.software.npm.tools` and
`envy.software.pypi.tools`; `modules/software/` owns their cross-platform
installation lifecycle, while CodeGraph MCP registration remains agent-owned.
The activation ensures and normalizes CodeGraph's Codex TOML table before and
after the upstream installer runs. This keeps registration idempotent when
Codex renders nested MCP table headers with indentation, which CodeGraph 0.9.7
otherwise misdetects and appends as a duplicate key.
Linux additionally installs Codex and RTK on both server and desktop machines,
and Cursor only when `envy.linux.option = "desktop"`. Darwin continues to
distribute Codex and ChatGPT through Homebrew casks.

## Remote agent relay

`envy remote-agent` (or its short form `envy ra`) launches an agent on another
machine. The remote agent is required to connect back over SSH and do the actual
work on the machine where the command was invoked. The command reads a
user-owned `~/.config/envy/remote-agents.toml`; authentication files, agent history,
and provider caches remain application-owned.

Create the commented starter file with:

```sh
envy remote-agent init
```

A connection using an SSH config `Host` entry looks like this:

```toml
[defaults]
target = "remote-agent"
agent = "claude"
system_prompt = "After connecting back, use the launcher host's local tools and filesystem for this task."

[connections.remote-agent]
method = "ssh"
host = "my-remote-host"       # resolved through ~/.ssh/config
args = ["-A"]
remote_identity = "~/.ssh/id_ed25519"
return_host = "this-machine-reachable-name"
return_user = "chi"
return_port = 22
# Optional wrapper for the remote-to-launcher connection:
# return_command = ["awesun", "ssh", "{user}@{host}:{port}", "--", "{remote_command}"]

[agents.claude]
command = ["claude"]
args = ["--dangerously-skip-permissions"]
prompt_args = ["--system-prompt", "{system_prompt}"]
```

`envy remote-agent start` first reads the remote public key (never the private key)
and appends it to the configured local `authorized_keys` file. `return_host` is
required because the remote agent must have a concrete SSH path back. The
command checks that path before starting the configured agent through an
interactive SSH session. `--dry-run` only inspects the planned connection and
command; key synchronization and the return-path check are always required for
a real launch. The local SSH server must accept the user's `authorized_keys`
file.

For a launcher machine that is reachable through an SSH wrapper such as Awesun,
set `connections.<name>.return_command`. It is a command vector with the
placeholders `{host}`, `{user}`, `{port}`, and `{remote_command}`. The same
command is used for the return-path preflight and exposed to the remote agent
as `ENVY_REMOTE_RETURN_COMMAND`, so the agent knows how to connect back.

For a configured command or a shell alias, use `method = "command"` with a
complete command vector, or `method = "alias"` with the alias name. Command
vectors may include `{remote_command}` when the remote command must be placed
at a particular position. The normal SSH method is preferred when an SSH
config entry is sufficient.

Provider arguments are deliberately configurable. A Codex installation can
use its own developer-instruction setting, for example:

```toml
[agents.codex]
command = ["codex"]
prompt_args = ["-c", "developer_instructions={system_prompt}"]
```

A custom provider can point at a remote configuration under `~/.config`:

```toml
[agents.my-agent]
command = ["my-agent"]
config = "~/.config/my-agent/config.toml"
config_args = ["--config", "{config}"]
prompt_args = ["--system-prompt", "{system_prompt}"]
```

Useful commands are `envy remote-agent list`, `envy remote-agent start <connection>`,
and `envy remote-agent start <connection> --agent <name> --extra-provider-arg`.
