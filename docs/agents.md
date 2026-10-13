# Agents module

`modules/agents` owns command-line/desktop agent installation, provider-specific wrappers, and reusable skills. Authentication files, history, caches, sessions, and plugin runtime state remain application-owned.

## Layout

```text
modules/agents/
├── default.nix          # shared provider defaults and skill selection
├── options.nix          # machine-owned agent instance schema
├── darwin.nix           # Darwin application distribution for nix-darwin
├── linux.nix            # Linux CLI/IDE distribution and runtime plugins
├── codex.nix            # Codex CLI adapter and runtime environment
├── linux/rtk.nix        # Linux-only RTK package
├── claude.nix           # Claude package, wrapper defaults, and ccli alias
├── generic.nix           # OpenAI-compatible and StepGo adapters
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
    targets = [ "codex" "claude-code" ];
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

The repository currently ships two external presentation skills. `ppt-master`
is linked from the `ppt-master/SKILL.md` subdirectory of
`Categorytyy/ppt-master`; it provides the PptxGenJS-oriented presentation
workflow and its supporting assets. `gpt-image2-ppt-skills` is linked from the
root `SKILL.md` of `JuneYaooo/gpt-image2-ppt-skills`; it generates image-led
presentations and can use an OpenAI-compatible image API. Both are pinned as
`tarball+https://api.github.com/.../tarball/...` inputs because a plain HTTPS
file input is treated as one file by Nix rather than an unpacked source tree.
The image skill expects `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and usually
`GPT_IMAGE_MODEL_NAME=gpt-image-2` at runtime. The Xikai endpoint is wired into
the agent wrappers, but whether it exposes the image generation API must be
verified by an actual image request before relying on that feature.

## Provider boundary

Each agent gets an adapter module (`codex.nix`, `claude.nix`, and generic adapters for compatible CLIs). The adapter owns package installation, stable command wrappers, protocol-specific environment names, and skill integration. `modules/llm` owns only non-sensitive endpoint metadata and sops credential declarations. A credential is a logical reference such as `stepfun`; its value is available only through an activation-time sops placeholder and is never a Nix evaluation-time string.

Machine-owned agent selections live under `envy.agents`:

```nix
envy.agents.codex = {
  adapter = "codex";
  endpoint = "stepfun-openai";
  credential = "stepfun";
  model = "step-3.7-flash";
};
```

Endpoints live under `envy.llm.endpoints` and contain only a URL and wire
protocol. Agent adapters translate those fields into the exact variables and
arguments expected by Codex, Claude Code, or a custom CLI. envY does not source
one global API-key environment file into every shell; each managed CLI wrapper
receives only its own rendered template.

The Claude wrapper deliberately invokes nixpkgs' `claude` wrapper, not its internal `.claude-wrapped` binary. Nixpkgs uses the outer wrapper to set required runtime environment variables and `PATH`; bypassing it would make this repository responsible for duplicating upstream packaging details.

Pi and Oh My Pi are separate agent commands. `pi` is the upstream
`earendil-works/pi` CLI and `omp` is the `can1357/oh-my-pi` fork, installed as a
companion command so the fork's extensions do not silently replace upstream Pi.
Both wrappers write provider metadata to their application-owned configuration
locations (`~/.pi/agent/models.json` and `~/.omp/agent/models.yml`) and select
`envy-pi/gpt-6.1-sol` by default. Each wrapper reads its own agent's endpoint,
model, API, and credential selections, with separate `envy-agent-pi` and
`envy-agent-omp` sops templates supplying the API key only at launch. Pi's model file contains an
`$OPENAI_API_KEY` reference. OMP's model file uses a command-backed
`apiKey` reference, and the wrapper also passes the resolved key through
`--api-key`. Neither file contains a decrypted secret.

The two OMP credential paths are intentional. OMP validates a custom provider's
model metadata before applying command-line options, so the command-backed
`apiKey` satisfies that validation while `--api-key` supplies the runtime
credential. When no credential is configured, the wrapper does not add
`--api-key`; endpoint and model selection still come from the normal agent
configuration. Changes to this adapter must preserve the invariant that
application files contain references or commands only, while the decrypted
value exists only in the launch-time sops template environment.

Pi and OMP select their wire API with `envy.agents.<name>.api`. The option
defaults to `openai-completions` (`/v1/chat/completions`); set
`openai-responses` to use `/v1/responses`. The base URL stays at the API root
(for example `https://models-proxy.stepfun-inc.com/v1`), not the full request
path. The gateway must support the selected API. For gateways that reject
GPT reasoning together with function tools on Chat Completions, choose
Responses rather than disabling reasoning. Pi and OMP can select different
APIs even when they share an endpoint.

```sh
envy config set envy.agents.omp.api openai-responses --json --yes
envy config set envy.agents.pi.api openai-responses --json --yes
envy apply
```

`envy config show --json` exposes these API fields and their choices for the
Settings frontend. `envy agent set` edits the same managed block, and
`envy agent add ... --api openai-responses` sets the API at creation. Invalid
API selections are rejected before writing. Reapply and restart the agent to
regenerate its model file; manually editing a generated file is not persistent.

Pi can opt in to additional thinking levels for endpoints that support them:

```nix
envy.agents.pi.more_thinking = true;
```

This machine option defaults to `false`. When enabled, envY builds the locked
Pi source with `modules/agents/patches/pi-ultra-thinking.patch` and declares
`xhigh`, `max`, and `ultra` in the custom model's `thinkingLevelMap`. Pi exposes
all three as independent choices in `/thinking` and accepts
`pi --thinking ultra`; the requested effort is sent unchanged to the endpoint.
The option makes these levels available without changing the startup level.
It does not add these capabilities to a model or gateway that lacks them.
OMP keeps its own upstream package and model metadata.

`envy config show --json` exposes the boolean for Pi, including its default.
Use `envy config set envy.agents.pi.more_thinking true --json --yes` (or
`envy agent set envy.agents.pi.more_thinking true --json --yes`), then
`envy apply` and restart Pi. Disabling it restores the upstream Pi package
and omits the extra model mappings. Keep any saved startup thinking level
within the upstream version's supported levels when disabling the patch.

The patch changes both the runtime libraries and bundled CLI. Its install
check covers CLI parsing, model metadata, settings, the thinking selector,
capability clamping, and Chat Completions/Responses request payloads without
sending a request. Rebuild it when updating the Pi input so upstream schema
or bundle changes cannot silently remove `ultra` support.

This API-key integration is distinct from an OpenAI Codex subscription, OAuth
session, or implementation-specific `codex-backend`. envY does not assume
Codex OAuth credentials can be reused as a generic API key.

Oh My Pi is a maintained Pi fork rather than a normal plugin package. Its
additional LSP, debugger, subagent, advisor, and execution features are
available through `omp`; application state and extensions remain under the
user's `~/.omp` directory. The `pi-extensions` repository was reviewed as a
candidate, but command-auditing extensions such as `toolwatch` are intentionally
not installed declaratively until their policy is selected explicitly.

## Shared agent configuration

The adapter modules use Home Manager's internal `config.agents.*` options for
implementation details. Machine-specific endpoint, model, credential, alias,
and argument choices belong to `config.envy.agents.*` and are editable through
the `envy agent` JSON/CLI boundary.

Useful commands include:

```sh
envy agent ls --json
envy agent show codex --json
envy agent add dsh --adapter openai-compatible-cli \
  --endpoint deepseek-openai --credential deepseek --model deepseek-chat
envy agent set envy.agents.dsh.model deepseek-reasoner --json --yes
envy agent set envy.llm.endpoints.deepseek-openai.baseUrl https://api.deepseek.com/v1
envy agent credential-set deepseek \
  --secret-path llm/deepseek/apikey --stdin
```

The JSON boundary is also the integration point for the Ratatui frontend. The
Settings view exposes evaluated endpoint and agent scalar fields; writes route
through `envy agent set`, so the TUI does not edit Nix files or decrypted
secrets directly. A future richer agent screen can use the same `agent add`,
`agent set`, and `agent credential-set` operations for composition and masked
secret input.

The two files contribute to different module systems. `default.nix` is imported
through the cross-platform `home.nix` composition root. `darwin.nix` is imported
through the repository's `darwin.nix` composition root and contributes only
Darwin Homebrew distribution policy. `flake.nix` does not import either feature
implementation directly.

The current shared configuration is intentionally explicit:

```nix
agents = {
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

The injected system prompt describes the topology explicitly: the remote runtime
host where the provider process is running, the launcher host that is the real
workspace, the envY connection profile, the selected provider and its config,
and the exact return-command template. It instructs the provider to connect back
before touching task files and to stop if that connection fails.

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
