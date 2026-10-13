# Software Policy And Search

`envy software` is the software policy boundary. `envy sw` is its only top-level
alias. Software is not a `config` subcommand, and there is no top-level
`envy search` command.

跨生态统一来源的设计与实施记录见 [Software 统一来源改造计划](software-sources-plan.md)。
可重复执行的端到端测试与验收范围见 [Software source E2E](software-sources-e2e.md)。
本页记录已实现的接口、来源约束与后端能力。

## Commands

| Command | Alias | Purpose |
|---|---|---|
| `envy software` | `envy sw` | List the evaluated policy |
| `envy software list` | `envy sw ls` | List checkbox state by manifest group |
| `envy software status` | `envy sw st` | Summarize groups and selections |
| `envy software add` | `envy sw add` | Ensure an item is effective on this machine |
| `envy software remove` | `envy sw rm` | Ensure an item is not effective on this machine |
| `envy software enable` | `envy sw en` | Remove a managed machine exclusion |
| `envy software disable` | `envy sw dis` | Add a managed machine exclusion |
| `envy software search` | `envy sw se` | Search available registries concurrently |
| `envy software audit` |  | Find stale, redundant, or ambiguous machine policy |
| `envy software why <item>` |  | Explain evaluated state and machine/shared ownership |
| `envy software cache status` |  | Inspect the exact registry identity index |
| `envy sw source ls/show/search` |  | Inspect declared and observed software sources |
| `envy sw source add/rm` |  | Preview and save machine sources or shared Nix registration |
| `envy sw source sync` |  | Verify metadata and invalidate this source's caches |

`envy sw ls --details` includes versions and canonical references. `enable` and
日常使用可以只提供软件名。envY 会先从 evaluated manifest 和精确 identity
index 判断兼容 group；唯一时自动选择，存在多个选择时在交互终端展示带人类
标签的 chooser。脚本和高级操作可以用 `--group` 消除歧义：

```bash
envy sw add zotero
envy sw rm zotero --clean
envy sw add zotero --group homebrew.system.cask
```

原有的精确 `<group> <item>` 形式保持兼容。`enable` 和 `disable` 仍直接操作
machine-local exclusion，因此要求 canonical group ID 和 stable item ID：

```bash
envy sw add homebrew.system.cask zotero
envy sw rm homebrew.system.cask zotero --clean
envy sw dis homebrew.system.cask zotero
envy sw en homebrew.system.cask zotero
envy sw se codex --source nix,npm
envy sw why zotero
envy sw audit --strict
```

`add` and `rm` are desired-state commands. Before writing, they display the
exact `include +/-` and `exclude +/-` plan plus the expected evaluated state,
then require confirmation. `--dry-run` stops after the plan and `--yes` accepts
it non-interactively.

- `add` removes the managed `exclude`; if no source contributes the item, it
  adds it to the managed `include`.
- `rm` normally adds the stable ID to the managed `exclude`.
- `--clean` then removes redundant managed `include`/`exclude` entries for the
  target ID, but only when the requested final state is preserved.
- An external exclusion blocks `add`; the command makes no partial change.
- Every applied plan is evaluated again. If the requested effective state is
  not reached, the managed block is rolled back.

Nix packages accept an attribute or `nix:` reference; other registry-backed
groups accept their package name or canonical reference. `--ref` associates a
custom stable ID with a canonical reference:

```bash
envy sw add hello --group nix.user.package
envy sw add codex --group npm.user.tool --ref npm:@openai/codex
```

A genuinely new managed include must be resolved before the machine file is
written. Existing manifest items can be enabled offline because their evaluated
identity is already known. New Homebrew, npm, PyPI, and native entries use the
fresh exact index first and otherwise perform a provider-specific exact lookup;
a fuzzy search match or a synthesized reference is not sufficient. Nix entries
validate a derivation in the project-locked input, including its stable name and drvPath.

`add --refresh` bypasses the exact identity index. `add --offline` never contacts
a registry and may use a stale positive identity, but it refuses a complete
cache miss. A definite not-found result and a temporarily unavailable provider
are reported separately, and neither state writes machine policy.

Shell completion is manifest-aware: `add` offers restorable managed exclusions
plus fresh exact-index identities for the selected ecosystem and kind; it never
contacts a registry just because Tab was pressed. `rm` offers known included
items, `dis` offers currently enabled items, and `en` offers envY-managed
exclusions. `why` completes explainable evaluated or machine-owned IDs across
groups (including stale exclusions), while `why --group` and `search --source`
complete canonical groups and providers. Completion reads an existing registry
index in SQLite read-only mode and does not create an empty cache.

`include` records that an active business module contributes an item;
`exclude` is a machine-level mask and always wins when both contain the same
stable ID. `en` removes only the envY-managed mask and then checks the evaluated
result:

- An included item that becomes effective is reported as enabled.
- An item no longer present in `include` is reported as stale-exclusion cleanup;
  removing the mask does not install it.
- An item still present in evaluated `exclude` is reported as excluded by
  another hand-written or imported policy.

## Manifest V3

`envy.machine.manifest.schemaVersion` is `3`; Python readers accept v2 and v3.
Software groups remain under `software.groups`, and definitions live under `software.sources`; the previous parallel `packages`, `homebrew`, `inclusions`,
and `exclusions` documents no longer exist.

Group IDs use exactly three dimensions:

```text
<ecosystem>.<scope>.<kind>
```

Examples:

```text
nix.user.package
nix.system.font
homebrew.system.cask
native.system.package
native.system.repository
npm.user.tool
pypi.user.tool
url.system.artifact
```

Platform support belongs in `platforms`. Artifact origin belongs in `ecosystem`.
The apply backend belongs in `installer`; for example a PyPI tool has
`ecosystem = "pypi"` and `installer = "uv"`.

Each group contains:

| Field | Meaning |
|---|---|
| `label` | Human-readable UI label |
| `optionPath` | Nix selection option root |
| `ecosystem` | Artifact namespace or registry |
| `platforms` | Platforms on which the selection mechanism applies |
| `scope` | `user` or `system` |
| `kind` | Package/tool/font/formula/cask/repository/artifact |
| `installer` | Home Manager, nix-darwin, Homebrew, npm, uv, or native backend |
| `editable` | Whether envY owns managed include/exclude writes |
| `reconcile` | Whether apply installs, upgrades, or removes entries |
| `selection` | Structured `include`, stable-ID `exclude`, and evaluated `effective` |

Items use `id`, `name`, optional `sourceId`, optional declared `version`, optional canonical `ref`, and
structured provider `parameters`. Exclusions always use `id`, so changing a
package version or display name does not invalidate machine policy. Query versions
and evaluated Nix derivation versions use `resolvedVersion`; a registry lookup
does not silently add a version pin.

Installer-specific parameters stay on the item instead of creating more option
trees:

| Installer | Parameters |
|---|---|
| Native Linux | `names.<manager>` overrides a package name for apt/dnf/pacman/zypper; `resolver = "current-kernel-headers"` selects the correct runtime kernel package |
| uv | `with` lists extra packages passed through `uv tool install --with` |
| URL artifact | `url`, `format`, and `packageName` describe the downloaded native package |

Canonical references identify an ecosystem object independently of its pinned
version, for example `npm:@openai/codex`, `pypi:ruff`, and
`homebrew:cask/iterm2`. `version` carries the pin separately.

### Nix source references

Nix derivations do not retain the attribute expression that created them after
evaluation, so envY never guesses a `nix:<name>` reference from the displayed
package name. Each Nix package contributor instead declares companion
`references` metadata next to `include`, keyed by the final `lib.getName`
package name:

```nix
envy.software.nix.packages.include = [ pkgs.git localTool ];
envy.software.nix.packages.references = {
  git = "nix:git";
  local-tool = "local:modules/tools/local-tool.nix#localTool";
};
```

Use `nix:<attr-path>` for nixpkgs, `flake:<input>#<attr>` for an external flake,
and `local:<path>` for a repository-owned derivation. `references` affects only
the manifest/TUI provenance display; `include` remains the installation policy.
The Nix modules reject a reference whose key is not present in their evaluated
include list. Machine-managed Nix additions written by `envy sw add` preserve
the same metadata automatically.

## Search Providers

Search is read-only and provider failures are isolated. Results are ranked by
exact/prefix match and whether the selected machine already manages the item.
Successful multi-provider results are cached for 15 minutes; `--refresh` bypasses
the cache.

The TUI keeps all available providers enabled so a slow registry never causes
information to disappear. It remains responsive because the request runs in a
background worker; repeated queries use envY's 15-minute query cache and exact
identity index. Provider failures are reported in the JSON `providers` list
instead of silently dropping that provider.

Search also writes every successful normalized result to the exact registry
index, even when another provider fails. This index is stored at
`$XDG_CACHE_HOME/envy/registry/index-v2.sqlite3` (or
`~/.cache/envy/registry/index-v2.sqlite3`), uses mode `0600` inside a `0700`
directory, and has a 24-hour positive TTL. Definite misses are cached for five
minutes to avoid repeated typo lookups. Query-result cache and exact identity
index are intentionally separate.

```bash
envy sw cache status
envy sw cache status --json
envy sw cache clean --yes
```

`list`, `status`, `audit`, and `why` support `--json` with a versioned top-level
schema for scripts and future frontends.

Desired-state mutations also expose a frontend-safe protocol:

```bash
envy sw add homebrew.system.cask firefox --dry-run --json
envy sw add homebrew.system.cask firefox --yes --json
envy sw rm homebrew.system.cask firefox --dry-run --json
```

These commands emit exactly one JSON document with `schemaVersion`, `command`,
`ok`, `data.result`, and the complete `data.plan`. JSON mutations require
`--yes` before writing; this lets a TUI render the preview first and explicitly
confirm the second invocation. The JSON path suppresses Rich tables and Git
commit guidance, while the normal interactive CLI remains unchanged.

| Source | Implementation |
|---|---|
| Nix | Project `legacyPackages.<system>.<input>` search and `softwareSourcePackages` exact evaluation |
| Homebrew | formula and cask searches |
| Native Linux | apt, dnf, pacman, or zypper detected at runtime |
| npm | `npm search --json` |
| PyPI | exact PyPI JSON registry lookup; uv remains the installer |
| Cargo | `cargo search` |
| Go | pkg.go.dev keyword search; there is no synthetic `go search` command |

The mirror environment is reused by command-based providers. A download mirror
does not necessarily expose a full-text search API, so PyPI and pkg.go.dev lookup
remain separate from artifact download configuration.

## Ownership

Business modules continue to own shared software entries. `modules/envy/`
defines selection types, computes effective values, and emits manifest groups.
`modules/software/` owns generic npm, uv, native package, and fixed-URL installer
behavior. The `ENVY MANAGED SOFTWARE` block assigns the existing
ecosystem `.include` and `.exclude` options directly. `add/rm` may update both;
setup and `en/dis` update only exclusions while preserving managed includes.
No workflow rewrites hand-maintained policy or shared module contributions.


## Unified sources

A source identifies the logical origin; a mirror changes transport, and a Nix
binary cache accelerates builds. Those are separate responsibilities. Runtime
sources expose known, registered, selected, and ready independently, along with
adapter capabilities and a context fingerprint. Observed external repositories
are read-only observations, never imported automatically into machine policy.

```bash
envy sw source ls --json
envy sw source show nix:nixpkgs --json
envy sw source search docker --ecosystem native
envy sw source add native:docker --dry-run --json
envy sw source sync native:docker --dry-run --json
envy sw se docker-ce --source native --source-id native:docker
envy sw add native.system.package docker-ce --source-id native:docker --dry-run --json
envy sw se hello --source nix --source-id nix:nixpkgs
envy sw add nix.user.package 'flake:pi#default' --dry-run --json
```

The local Pi wrapper retains its local reference and separately declares its
upstream source dependency. Adding the raw upstream package can
conflict with the wrapper's stable package name; envY rejects the conflict.
Default providers search sources that are selected or ready. Repeat
`--source-id` on search to choose concrete origins; `--source` still filters
providers. Native manager aliases must match the current manager.

The v2 SQLite identity key includes source ID, fingerprint, ecosystem, kind,
and reference. Fingerprints cover the project lock graph, platform, native
source set and trust files, registry transport, or actual Homebrew tap revision.
A changed context cannot reuse an old identity, including in offline mode.
Definite misses and provider failures remain separate. Metadata sync invalidates
both the identity index and this source's query cache. Nix sync reads metadata
with lock updates disabled; updates use `envy update inputs <name>`.

Source mutations retain JSON envelope v1 and support `--dry-run`, `--json`,
and `--yes`. Shared Nix registration reports its flake diff, lock change scope,
and validation across every machine:

```bash
envy sw source add nix:extra-tools --ref github:OWNER/REPOSITORY --dry-run --json
```

Registration manages only marked input/binding blocks, locks the input, checks
all machines and both supported systems, and restores flake.nix/flake.lock on
failure. Removal rejects consumers and never takes over handwritten inputs.
Registered package providers currently expose their `packages.<system>`
outputs; nixpkgs uses the project's package set. Inputs without packages on both
project systems fail shared registration.

Custom public sources use a JSON definition file. envY writes it into the
selected host's separate `ENVY MANAGED SOURCES` block:

```bash
envy sw source add native:vendor --definition vendor-source.json --dry-run --json
```

A native definition contains `label`, `ecosystem = "native"`, `type =
"native"`, Linux platforms, system scope, and per-manager parameters. APT
parameters are uri, suites, components, optional architectures, keyUrl, and a
full pinned signing keyFingerprint. The only runtime placeholders are
`{distro}`, `{codename}`, and `{architecture}`. Source/package additions
select required repositories in the same host transaction; explicit source
exclusions block them. Removing a source with effective dependents is rejected.

## Native repositories and artifacts

APT discovery and installation use the same effective source reader. When the
envY mirror exists, confidently identified Ubuntu/Debian entries are replaced,
while vendor and unknown sources are retained. External files stay owned by the
system; temporary descriptors do not widen source-file access. Authentication
continues through APT's existing configuration and is never exported in JSON.

Repository activation verifies the pinned primary key and manages only
envY-named source/keyring files. Equivalent external APT repositories remain
external. Docker and Waydroid now declare their repositories and packages in
their owning modules instead of executing downloaded installer scripts.
Their first maintained vendor definitions support Ubuntu/Debian APT.

Source-constrained APT package installation reads a private signature-verified
index, downloads the selected repository artifact, verifies its hash and native
metadata, then installs the local file with dependencies from the effective
source set. Private metadata refresh disables system caches and update hooks.
Existing installed native packages remain installed; policy removal is not an
uninstall, and no general native upgrade or migration of installed provenance
is claimed.

DNF and Zypper support envY-owned repository descriptors and pinned signing
keys. Package search/exact resolution works through the observed system source
set, but repository-constrained search/install and private metadata sync are
reported unsupported on these managers. Pacman uses the existing sync database;
envY does not run an automatic partial or full system upgrade and reports
repository configuration unsupported. Cargo and Go remain discovery-only.

A URL artifact definition uses ecosystem url/type artifact and supplies a
public fixed URL, deb/rpm format, packageName, full SHA256 (hex or SRI), and
optional version, architecture, and bootstrapRepository. Add the source, then
add its software selection:

```bash
envy sw source add url:vendor-app --definition vendor-app.json --dry-run --json
envy sw add url.system.artifact url:vendor-app --source-id url:vendor-app --dry-run --json
```

Before saving a new artifact include, envY verifies content hash, package name,
version when declared, and native architecture. Apply repeats verification.
APT installs deb; DNF/Zypper install rpm. Legacy direct artifacts retain their
definitions and receive package metadata checks; Doctor reports missing pinned
hashes. Installation observes repository-file changes and checks a declared
bootstrap repository without silently adopting it. System package scripts and
package database changes are not covered by file-transaction rollback.

TUI Configure includes Sources; Enter previews a repository selection and s
requests metadata sync. Search shows concrete origins and availability and
passes source IDs back to the CLI. Setup shows source annotations and reuses
the repository checkbox group; custom definitions and shared input registration
remain in the scoped source CLI. Doctor reports pending sources, unsupported
legacy native version constraints, and unpinned legacy artifacts.
