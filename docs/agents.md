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
├── pi.nix              # Pi wrapper, prompt, and provider metadata
├── prompts.nix         # shared and provider-specific system prompt loader
├── prompts/            # shared, Claude, Pi, and Codex instructions
├── pi-extensions.nix   # selected extensions from the locked upstream Pi source
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

1. `agents.skills.active` selects shared catalog entries, filtered by each entry's supported targets. `envy.agents.<name>.skills` adds compatible catalog entries to that selection. Home Manager links the resolved set into `~/.codex/skills`, `~/.claude/skills`, `~/.stepgo/skills`, and `~/.pi/agent/skills` during activation. Instances of the same adapter share a directory and therefore share the union of their selections; `claude` and `claude-code` identify the same skill target.
2. For a discovered skill, the agent sees its `name` and `description` metadata first. It reads the `SKILL.md` body only when the task triggers that skill, then reads referenced resources as needed.

This keeps agent capabilities declarative while preserving progressive, task-driven context loading. Changing the physically discoverable set requires a rebuild; ordinary task-level loading does not.

`envy agent show <name>` and `envy config show --json` report the resolved set in
`skills`, and the machine's explicit additions in `configuredSkills`. These are
the evaluated desired state; application-installed skills and project-local
skills are outside this catalog. Until `envy apply` completes, the currently
installed links can differ. Unknown or incompatible catalog selections fail
configuration validation.

List fields use JSON arrays on the CLI, for example:

```sh
envy agent set envy.agents.pi.skills '["animate", "break-ui"]' --yes
envy agent set envy.agents.pi.skills '[]' --yes
```

An empty additions list retains the shared selection. To remove a shared skill,
change `agents.skills.active` in its owning module. The same JSON-array syntax
works for `args` and `aliases`; nullable agent fields accept `null` to clear a
selection.

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

The shared selection also includes seven frontend skills from
`emilkowalski/skills` and `vercel-labs/agent-skills`. Their complete directories
are pinned through the `emil-skills` and `vercel-agent-skills` non-flake inputs
and linked for Codex, Claude Code, and Pi. See
[the skill and Pi extension selection guide](agent-skills.md) for the research,
selected capabilities, commands, and update procedure.

## Provider boundary

Each agent gets an adapter module (`codex.nix`, `claude.nix`, and generic adapters for compatible CLIs). The adapter owns package installation, stable command wrappers, protocol-specific environment names, and skill integration. `modules/llm` owns only non-sensitive endpoint metadata and sops credential declarations. A credential is a logical reference such as `stepfun`; its value is available only through an activation-time sops placeholder and is never a Nix evaluation-time string.

Each adapter receives the shared prompt from `prompts/common.md` and may append provider-specific instructions from `prompts/claude.md`, `prompts/pi.md`, or `prompts/codex.md`. Claude uses `--system-prompt` and preserves the `CCLI_SYSTEM_PROMPT` and `~/.config/ccli/prompt` overrides. Pi uses `--append-system-prompt`, so Pi's built-in system prompt and project context remain available. Codex receives the same rules through `developer_instructions` in its configuration override.

Pi enables `+codemode` in its adapter defaults and sets `codemode.mode = "only"`.
This keeps the normal `read`, `bash`, `edit`, and `write` tools callable from
scripts while routing active Pi tool calls through codemode. A machine can
override the complete `defaultTools` list or `codemode.mode` through
`envy.agents.pi.additionalSettings`. The Pi prompt keeps pure conversational
replies tool-free and requires codemode for repository-scale, parallel, and
multi-step work.

Machine-owned agent selections live under `envy.agents`. Provider-specific settings that are outside the fixed schema belong in `additionalSettings`:

```nix
envy.agents.codex = {
  adapter = "codex";
  endpoint = "stepfun-openai";
  credential = "stepfun";
  model = "step-3.7-flash";
};

envy.agents.pi.additionalSettings = {
  defaultTools = [ "+codemode" ];
  codemode.mode = "only";
};
```

`additionalSettings` is recursively merged during activation into the writable
user configuration file. Existing application fields are retained, nested
attribute sets merge by key, declared values win on conflicts, and lists are
replaced as a whole while preserving their declared order. For example, the
Pi setting above enables Codemode while retaining Pi's other default tools and
routing their model-facing calls through the Codemode script runner.
The same option is available to every adapter that declares a settings file. Adapter modules provide the file format and path; an agent may override that metadata with `additionalSettingsFile`. The provider model files are merged with the same rules, so runtime-added
providers survive a reapply. Object containment helpers used by module checks
compare nested attribute sets by key/value and ignore key order; lists remain
ordered.

Endpoints live under `envy.llm.endpoints` and contain only a URL and wire
protocol. Agent adapters translate those fields into the exact variables and
arguments expected by Codex, Claude Code, or a custom CLI. envY does not source
one global API-key environment file into every shell; each managed CLI wrapper
receives only its own rendered template.

The Claude wrapper deliberately invokes nixpkgs' `claude` wrapper, not its internal `.claude-wrapped` binary. Nixpkgs uses the outer wrapper to set required runtime environment variables and `PATH`; bypassing it would make this repository responsible for duplicating upstream packaging details.

`pi` is the upstream `earendil-works/pi` CLI. Its wrapper writes provider
metadata to `~/.pi/agent/models.json` and selects `envy-pi/gpt-6.1-sol` by
default. It reads the agent's endpoint, model, API, and credential selections,
with the `envy-agent-pi` sops template supplying the API key only at launch.
The model file contains an `$OPENAI_API_KEY` reference, never a decrypted
secret.

Pi selects its wire API from the selected endpoint protocol unless
`envy.agents.<name>.api` supplies an explicit override. The mappings are:

- `openai-compatible` -> `openai-completions` (`/v1/chat/completions`)
- `anthropic-compatible` -> `anthropic-messages` (`/v1/messages`)
- an explicit `openai-responses` -> `/v1/responses`

The base URL stays at the API root (for example
`https://models-proxy.stepfun-inc.com/v1`), not the full request path. The
gateway must support the selected API. For gateways that reject GPT reasoning
together with function tools on Chat Completions, choose Responses explicitly.
For an Anthropic endpoint, envY generates `ANTHROPIC_BASE_URL` and
`ANTHROPIC_API_KEY`; for OpenAI-compatible endpoints it generates the
corresponding `OPENAI_*` variables. Custom Anthropic models also receive
`compat.forceAdaptiveThinking = true`, so Pi sends `thinking.type = adaptive`
and `output_config.effort`, as required by current Claude models and gateways.

```nix
envy.llm.endpoints.my-anthropic = {
  baseUrl = "https://api.anthropic.com";
  protocol = "anthropic-compatible";
};
envy.agents.pi.endpoint = "my-anthropic";
```

Use an explicit override when a gateway exposes a protocol that differs from
its endpoint metadata:

```sh
envy config set envy.agents.pi.api openai-responses --json --yes
envy apply
```

`envy config show --json` exposes these API fields and their choices for the
Settings frontend. `envy agent set` edits the same managed block, and
`envy agent add ... --api anthropic-messages` sets an explicit API at creation.
Set the field to `null` to return to protocol-based inference. Invalid API
selections are rejected before writing. Reapply and restart the agent to
regenerate its model file; manually editing a generated file is not persistent.

Pi can opt in to additional thinking levels for endpoints that support them:

```nix
envy.agents.pi.moreThinking = true;
```

This machine option defaults to `false`. When enabled, envY builds the locked
Pi source with `modules/agents/patches/pi-ultra-thinking.patch` and declares
`xhigh`, `max`, and `ultra` in the custom model's `thinkingLevelMap`. Pi exposes
all three as independent choices in `/thinking` and accepts
`pi --thinking ultra`; the requested effort is sent unchanged to the endpoint.
The option makes these levels available without changing the startup level.
It does not add these capabilities to a model or gateway that lacks them.

`envy config show --json` exposes the boolean for Pi, including its default.
Use `envy config set envy.agents.pi.moreThinking true --json --yes` (or
`envy agent set envy.agents.pi.moreThinking true --json --yes`), then
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

Upstream Pi additionally loads the official questionnaire, todo, tools, and
handoff extensions from the same locked source as its executable.
`pi-extensions.nix` owns four `envy-*.ts` links under
`~/.pi/agent/extensions/`; Pi keeps ownership of settings, installed package
state, and session data. `/handoff <goal>` makes a request to the current model
to generate a draft for a new session; the user reviews and submits it.

## Retired Oh My Pi configuration

Oh My Pi (OMP) is no longer a supported envY agent. envY no longer installs
`omp`, generates its provider configuration, or selects skills for
`~/.omp/agent/skills`.

`envy config check` identifies legacy OMP settings as redundant. Run
`envy config refine` to remove `envy.agents.omp.*` from the machine's managed
configuration block, along with any other named agent instance whose adapter
is `oh-my-pi`, then run `envy apply` to update the installed environment.
Shared endpoint and credential definitions are preserved because other agents
may use them. The migration also preserves application-owned auth, history,
and other state in `~/.omp`; it does not delete that directory.

Legacy configuration rules are kept in `resources/scripts/envy/legacy.py`.
Runtime modules expose only the current schema; `envy config refine` is the
single migration boundary. For example, it rewrites the old
`envy.agents.<name>.more_thinking` assignment to
`envy.agents.<name>.moreThinking`, and reports a conflict if both spellings
have different values.

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

Skill selection is explicit in `modules/agents/default.nix`; for example, the
research subset is:

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
