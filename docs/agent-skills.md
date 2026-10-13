# Selected agent skills and Pi extensions

Reviewed on 2026-10-13. This selection adds frontend design and everyday
development capabilities to the existing research and presentation workflows.
`modules/agents/default.nix` selects skills, `skills/catalog.nix` maps them to
upstream directories and agents, and `pi-extensions.nix` selects executable Pi
extensions.

## Skills

The selected catalog entries are enabled for their declared targets (including
Codex, Claude Code, and Pi where supported). Their selected directories
contain Markdown/JSON resources and require no additional API keys or installed
skill dependencies. They provide instructions; browser rendering and framework
execution still depend on the tools available in the working project.

| Skill ID | Use it for | Example request |
| --- | --- | --- |
| `emil-design-eng` | UI details, component interactions, accessibility, and animation decisions | Review this dialog's interaction and visual details. |
| `animate` | Implementing purposeful transitions with reduced-motion support | Add an interruptible entrance/exit transition to this drawer. |
| `pick-ui-library` | Choosing component libraries; companion workflow referenced by `animate` | Use pick-ui-library to choose an accessible dropdown library for this project. |
| `animation-performance` | Diagnosing frame drops, layout/paint cost, and per-frame React updates | Find why this animation stutters on mobile. |
| `break-ui` | Long text, empty data, large counts, Unicode, and other realistic edge cases | Stress-test this member list with worst-case data. |
| `vercel-react-best-practices` | React/Next.js waterfalls, bundles, rendering, and server performance | Audit the data fetching and rendering performance of this page. |
| `vercel-composition-patterns` | Reusable component APIs, composition, and state ownership | Refactor this component's many boolean props into a composable API. |

The first five come from [Emil Kowalski's skills](https://github.com/emilkowalski/skills),
reviewed at [`2d228630ba14`](https://github.com/emilkowalski/skills/tree/2d228630ba1446365f614a108cb8252f1feda202).
The last two come from [Vercel agent skills](https://github.com/vercel-labs/agent-skills),
reviewed at [`063bee94c3f4`](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278).
Both selections are MIT licensed; Emil provides a root LICENSE, and Vercel
declares MIT in its README and the selected skills' frontmatter.

The Vercel upstream directories are `skills/react-best-practices` and
`skills/composition-patterns`, while their declared skill names carry the
`vercel-` prefix. Keep those names in the catalog. Link complete directories:
`animate` needs `RECIPES.md`, `break-ui` needs `CATALOG.md`, and the Vercel skills
include `rules/` and `AGENTS.md`.

Agents discover each skill's metadata and load its full instructions when
relevant. To request one explicitly, name its ID in the task; Pi also supports
commands such as `/skill:animate`.

`animate` delegates complete component selection to `pick-ui-library`, so the
latter is included as a companion. Its upstream `disable-model-invocation: true`
metadata and explicit-use description are preserved. Invoke it by name, or use
`/skill:pick-ui-library` in Pi; clients differ in how they enforce this metadata.

## Pi extensions

The four extensions come from the MIT-licensed
[official Pi examples](https://github.com/earendil-works/pi/tree/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/examples/extensions)
at the existing `pi` input's revision, `ea448f454af474f3e2fe9733b331e17b0662f5d6`
(Pi 1.1.0 at review time). Sharing the input with the executable keeps their API
version aligned. Their imports are provided by Pi's extension loader; no
`pi install` or activation-time npm install is required.

| Extension | Entry point | Behavior |
| --- | --- | --- |
| `questionnaire.ts` | Agent's `questionnaire` tool | Presents one or multiple questions with choices and free text in interactive mode. |
| `todo.ts` | Agent's `todo` tool; `/todos` | Tracks tasks and reconstructs their state from the current session branch. |
| `tools.ts` | `/tools` | Lets the user enable or disable tools for the current session. |
| `handoff.ts` | `/handoff <goal>` | Uses the current model to draft a focused context transfer; opens an editor and prepares a new session for the user to submit. |

Home Manager owns only the four `~/.pi/agent/extensions/envy-*.ts` links. Pi
continues to own `settings.json`, packages, auth, history, and all extension
session state. Handoff sends conversation context to the already selected
provider only when invoked and incurs an additional model request.

The actual Nix-evaluated links loaded successfully with the installed Pi 1.1.0
loader in an isolated temporary agent directory: all selected skills and four
extensions, zero skill diagnostics or extension warnings/errors, and no
collisions with built-in commands/tools or each other. All files in the selected
skill directories remained readable through their links, including companion
documents and rule references. This smoke check blocked networking and used no
model requests. Interactive behavior and provider responses remain runtime
checks.

## Other candidates reviewed

| Candidate | Reason to leave it out of the default selection |
| --- | --- |
| Emil `mobile-native`, `apple-design` | Useful additions for a concrete mobile/Apple project; the initial selection focuses on general web UI. |
| Emil `review-animations`, `prototype` | Use `disable-model-invocation` metadata whose handling varies across agents and are not required companion workflows for this selection. |
| Vercel `web-design-guidelines` | Downloads rules from another repository's moving `main` branch at invocation time, so pinning the skill alone does not pin its rules. |
| [Anthropic skills](https://github.com/anthropics/skills) | Document/research workflows overlap existing capabilities; `mcp-builder` is a useful future addition when developing an MCP server. |
| [Pi skills](https://github.com/badlogic/pi-skills) | Browser/search helpers need separately packaged npm dependencies; some search/Google integrations also need credentials or OAuth. A source symlink alone is insufficient. |
| [Agent Browser](https://github.com/vercel-labs/agent-browser) | Needs its CLI and browser lifecycle packaged alongside the skill. |
| Official Pi `plan-mode` | Registers `/todos`, conflicting with `todo.ts`, and references `brave-search`. Its shell restrictions are not a security sandbox. |
| [pi-subagents](https://github.com/nicobailon/pi-subagents) | Promising maintained package with additional dependencies and background/session behavior; requires a separate packaging and runtime review. |

## Apply and update

After reviewing the machine's pending configuration changes, run `envy apply`
to create the managed links. Restart the agents afterward; an already running
Pi can use `/reload`. Repository edits alone do not change the current agent
session's available skills.

Update only the relevant skill inputs:

```sh
nix flake update emil-skills vercel-agent-skills
nix flake check path:. --no-build
envy check --all
```

Review upstream instruction changes and the resulting `flake.lock` diff before
applying. Updating `pi` also updates its four extensions; recheck extension
loading and command/tool collisions whenever upgrading Pi. `flake.lock` is the
authoritative revision record; the revisions above identify the reviewed
baseline.
