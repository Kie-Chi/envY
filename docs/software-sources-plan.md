# Software 统一来源改造计划

日期：2026-10-12。检查基线：`cb2fa10`。状态：六个阶段已实施，交付与验证记录见文末。

现状补注（2026-10-13）：OMP 已从 envY agent 和共享软件来源中移除。下文的 OMP/oh-my-pi 记录保留为实施历史；当前配置与迁移方式见 [agents.md](agents.md#retired-oh-my-pi-configuration)。

envY 将在 software 内引入跨生态的来源目录。来源定义提供哪些软件、如何发现和验证软件；现有 manifest 分组继续表达机器选择，安装后端负责应用。搜索、精确解析、缓存和安装必须引用同一个来源身份。

## 1. 目标与现状

目标用户流程：选择软件 → 解析其具体来源 → 展示所需来源和软件的变更 → 保存机器策略 → plan/apply。来源尚未准备好时，计划说明前置步骤；来源不存在或无法验证时，保持机器策略不变。

当前缺口：

- Nix 搜索和新增包解析固定使用 registry 的 `nixpkgs`，应用使用项目锁定的 input，两者可能不一致；第三方 flake 没有统一进入搜索。
- Native 已有 `native.system.package`，但没有仓库管理分组。APT 搜索与安装的 source set 不一致；启用 envY 镜像后，安装通过 `sourceparts=-` 排除了第三方仓库。
- Native 精确解析没有核验返回包名；APT 的 `No packages found` 被误判为 unavailable。Zypper 表格输出被 DNF 风格解析器丢弃。
- `url.system.artifact` 只支持 APT 上的 `.deb`，不能通过 software 命令新增任意安装文件。
- Native 和 URL artifact 当前主要保证首次安装，不能把软件策略的移除解释为系统包卸载。

前次检查的 software 与 registry index 相关 80 个测试通过，但缺少上述后端解析和来源一致性覆盖。这是基线记录，不代表计划中的能力已经通过验证。

## 2. 保持的架构约束

- 保留 `<ecosystem>.<scope>.<kind>` 分组、stable item ID、include/exclude 和现有 Nix `references`。引入来源不会自动改写包 ID、排除项或手写配置。
- 机器选择、自定义来源和非敏感参数保存在 `hosts/<platform>/<id>.nix`；`.device-label` 只保存设备身份。
- 业务模块声明自己拥有的共享来源和软件。`modules/envy/` 只定义 schema、聚合与 manifest，不承载中央软件目录。
- Nix 软件继续通过 derivation include 和既有聚合器安装。统一来源不是绕过 Home Manager/nix-darwin 的第二套 Nix 安装器。
- Python CLI 拥有解析、规划和变更逻辑；Rust TUI 只消费 JSON。Setup 复用同一套来源与软件策略助手。
- 继续使用共享 `master`。新增或更新 Nix input 属于项目共享变更，必须验证所有机器。
- 镜像、代理与二进制缓存继续由 mirrors 等原有模块管理。来源身份与下载 endpoint 分开。
- 认证信息通过 sops 或应用现有认证机制使用；Nix eval、来源目录、日志和 JSON 不包含密钥、带凭据的 URL 或认证配置正文。

## 3. 四个核心对象

| 对象 | 职责 | 示例 |
|---|---|---|
| Source | 一个具体的软件来源，拥有稳定身份、来源参数和适用范围 | 锁定的 nixpkgs、Docker APT 源、Homebrew tap、npm registry |
| Provider | 读取某类来源的适配器 | Nix flake、APT、DNF、Homebrew、npm、PyPI |
| SoftwareItem | 来源中的一个软件对象，包含引用和机器选择身份 | nixpkgs 中的 hello、Docker 源中的 docker-ce |
| Backend | 执行应用、配置来源或安装软件 | Home Manager、APT、Homebrew、npm、uv |

Source 是统一抽象；不同生态继续保留各自参数与执行方式。Nix registry 是名称映射；Nix binary cache 是构建产物来源，不作为可增加软件目录的 repository。

来源目录要区别以下事实，不能用一个 enable 开关混在一起：

- **known**：envY 有经过维护的定义。
- **registered**：来源已登记，例如 flake input 已锁定，或主机已有 APT source 文件。
- **selected**：机器策略选择了该来源，或有效软件依赖它。
- **ready**：当前主机可以用它解析、搜索或安装；这是运行时探测结果。

软件来源定义与机器选择分开。Homebrew tap 和新增的 native repository 分组是选择入口，统一目录记录这些入口对应的 Source；同一份选择不在另一个通用选项中重复保存。

## 4. 数据契约与所有权

### 来源定义

新增通用 `envy.software.sources` 定义 schema，由业务模块贡献；机器可以在自己的 host 文件内声明自定义来源。公共字段包括 `id`、`label`、`ecosystem`、`type`、`platforms`、`scope`、`parameters`。`parameters` 按生态/type 校验，不继续以任意 attrs 代替全部验证。

稳定来源 ID 使用 `<ecosystem>:<name>`，例如 `nix:nixpkgs`、`nix:oh-my-pi`、`native:system`、`native:docker`、`homebrew:homebrew/core`、`npm:npmjs`、`pypi:pypi`。它标识逻辑来源；切换镜像不改 ID。不同发行版的 native 来源通过后端参数和运行时上下文区分。

定义需要记录其所有者：业务模块、机器配置、共享 flake 登记或主机已有配置。外部来源的运行时发现只形成观测结果；不会自动导入机器配置。

同一 sourceId 的生态、类型或绑定信息冲突时求值失败。Nix 来源的 input 与锁定信息由共享项目登记决定，host 中的来源定义不能重绑定同一个 ID 来绕过项目锁。

适配器补充 `capabilities`、锁定信息、解析状态与 fingerprint。能力包括 search、exactResolve、enumerate、indexRefresh、configure，以及 sourceConstraint、versionPin 等约束是否可执行。能力来自实现和探测，不由配置中的布尔值虚构。

### Manifest v3

新增 `software.sources`，保留 `software.groups`。软件条目增加可空的 `sourceId`；provider report 增加具体来源、上下文和失败原因。来源定义、机器期望状态和主机观测状态分别表达，避免 Nix eval 读取主机软件源或网络。

Nix package selection 保留 `references`，必要时增加与最终包名对应的 `sourceIds` 元数据。直接引用可通过明确的引用语法绑定来源；本地 wrapper 的引用仍为 `local:<path>`，上游 flake 作为依赖记录，不把 wrapper 伪装成原始包。

`version` 继续表示声明的版本约束；查询得到的版本放在解析结果的 `resolvedVersion`，不会在新增软件时无条件成为 pin。既有非空 `version` 保留。后端不支持执行版本约束时明确报告，不能默默忽略。

读取端先支持 manifest v2/v3，再切换输出到 v3。CLI 的 JSON envelope 版本与 manifest 版本独立，现有 envelope v1 保持兼容。Search 的旧 `results/providers/query/platform` 字段先保留，新增来源字段；其 envelope 统一另做兼容迁移。

兼容读取规则如下；来源注释不自动写回旧机器文件：

| 旧数据 | 兼容方式 |
|---|---|
| `nix:<attr>` | 绑定项目锁定的 `nix:nixpkgs` |
| `flake:<input>#<attr>` | 仅在 input 已明确登记时绑定对应来源；未知 input 报告未登记 |
| `local:<path>` | 保留本地来源与引用；明确声明的上游依赖单独记录 |
| Homebrew formula/cask 字符串 | 保留选择；通过真实 tap 元数据补充来源，不能仅凭包名猜 tap |
| `native:<name>` 与 `names.<manager>` | 保留 stable ID 与各 manager 名称；运行时解析有效来源中的真实候选 |
| npm/PyPI 既有条目 | 绑定原先使用的逻辑 registry，并保留版本与安装参数 |
| URL artifact 既有参数 | 保留现有定义；缺少验证信息时报告其验证等级，新增条目采用新校验要求 |

旧 native 条目中的非空版本字段不在迁移时删除或伪装成已执行的 pin。未执行的旧约束单独展示，不使来源 schema 迁移本身阻断旧机器求值；新的不受支持的版本约束在保存前拒绝。

### 配置写入

- 已知 native 来源的选择写入新增的 `envy.linux.software.native.repositories.include/exclude`，对应 `native.system.repository`。
- Homebrew 来源继续使用现有 repositories/taps 选择，不生成第二份 tap 列表。
- 自定义来源定义写入 host 内独立的 `ENVY MANAGED SOURCES` 块；现有 `ENVY MANAGED SOFTWARE` 仍只承担直接的 include/exclude 选择。
- 来源定义与依赖软件的选择在一次文件事务中保存，并重新求值验证。不会修改 markers 外的手写政策。
- Nix 新 input 的登记属于共享项目事务，不能藏在机器级 source enable 中。

## 5. 来源绑定、搜索与缓存

Provider 接口统一为只读的来源发现、search、resolveExact 与临时索引获取；允许更新 envY 缓存，不拥有机器策略或系统来源修改逻辑。Planner 构建来源配置、依赖和软件选择计划；Backend 执行来源配置、显式索引同步与安装。各层消费同一个已解析来源和软件计划，避免分别选择 endpoint 或候选包。

Nix manifest 输出声明的目录与期望状态；运行时 catalog 再合并主机已有来源和就绪状态。外部 source 文件、发行版探测和网络结果只在后者读取。

默认搜索使用当前机器已选择或已可用的来源。已知但未配置的来源可以在来源目录中搜索；不默认向所有已知厂商仓库发送软件查询。用户明确选择某个未配置来源时，支持只读的临时索引获取，并展示前置条件；未实现该能力的适配器准确返回 not-ready/unsupported。

保留 `sw se --source nix,native` 的 provider 过滤语义，新增可重复的 `--source-id` 过滤具体来源。`apt/dnf/pacman/zypper` 兼容别名需核验当前 manager，不能把不匹配的别名静默转换成另一后端。

精确解析必须核验 canonical name、kind、平台、实际来源及可执行的候选约束，拒绝通配符、选项和模糊别名。不同来源的同名包返回独立结果，添加时需要消除歧义。若后端无法保证指定来源，拒绝该 source constraint，并给出明确原因。

保留现有 stable ID 和 exclusions 语义。同一机器中的包名/derivation 冲突继续由求值拒绝；不能因为引入 sourceId，就默认允许安装两个冲突版本。

Registry index 升级为来源限定的 v2 索引，键至少包含 sourceId、source/context fingerprint、kind 和 canonical ref/name。Fingerprint 覆盖锁定 revision/hash、系统与架构、发行版、仓库参数、信任信息及有效来源集合；查询缓存还考虑实际访问 endpoint。

旧索引不作为新来源绑定的验证证据；首次在线查询重建。`--offline` 只接受上下文匹配的正向缓存，可按既有规则接受过期结果。来源变化立即使相关正向和负向结果失效。一个来源失败仍保留其他来源的成功结果。

## 6. 各生态的实施方式

### Nix

- 显式登记提供软件包的 inputs；不把所有 flake input 自动当作软件目录。
- 在项目 flake 中提供使用锁定 inputs 的软件来源/包集合解析入口。搜索、精确解析和生成的机器 include 都绑定同一个 input 与目标 system，不回退到全局 registry 的同名来源。
- `nix:<attr>` 保留为项目 nixpkgs 的兼容引用；`flake:<input>#<attr>` 支持已登记的第三方输入；`local:<path>` 保留本地 derivation 语义。
- 精确解析验证可用输出与平台，而不只读取 pname。机器 managed include 的受限 writer/parser 扩展为明确的来源包表达式，不执行任意用户输入表达式。
- 新 input 的 CLI 登记使用 `flake.nix` 内受控的 managed inputs 块，以及版本化的来源绑定声明；手写 inputs 不被重新格式化或接管。
- 登记/删除/更新事务覆盖 input 声明、来源绑定和 `flake.lock`，复用全机器检查与文件回滚。来源被其他机器或模块依赖时禁止删除；`follows` 与跨平台输出一起验证。不会自动提交或推送。
- 首轮先接入 nixpkgs、pi、oh-my-pi，随后开放受控的新 input 登记。

### Native / APT

- 原生来源参数在运行时根据 OS release、manager 和架构解析；这些观测不成为第二个机器配置层。
- 提供一个共享的 `EffectiveSourceSet` 解析器。搜索、候选版本查询、index refresh、包安装和本地安装文件的依赖解析都使用它。
- 在 china 模式，以 envY 镜像替换可明确识别的发行版仓库条目，同时保留已选择的厂商来源与系统已有的第三方仓库。无法分类的外部来源保留并说明，避免仅因分类失败而隐藏来源。
- 使用 envY 自己的文件/临时来源目录表达有效集合，读取外部配置但不覆盖、重命名或删除系统拥有的 source 文件。复用系统 APT 认证机制，不把认证内容复制进 manifest 或日志。
- 来源注册生成 envY 自有 `.sources` 与 keyring，验证官方来源信息与公钥指纹；不使用 `apt-key` 或通用 `curl | sh` 来代替声明式来源定义。
- 依赖顺序明确为：镜像策略 → 仓库与 keyring → 索引同步 → native 包和 artifact 依赖安装。
- 对已知未激活来源，精确解析可在私有临时目录获取并验证签名索引，不需要先写入系统 sources。无法验证时不保存来源或包策略。
- 首批定义放在各自拥有软件的业务模块中；优先接入系统源与项目已有的 Docker、Waydroid 来源。其他厂商来源在官方信息验证后逐个加入。
- 主机已有同一来源时先展示 external 状态，避免重复配置；明确接管时只创建 envY 自有声明，保留外部文件并报告重复项。

### DNF / Pacman / Zypper

- 各自实现搜索与精确解析，使用稳定/结构化接口优先，文本解析固定 locale 并覆盖实际输出差异；DNF4/5 分开验证。
- 仓库文件和信任配置只管理 envY 自有部分；不自动接管第三方配置。
- 移除 Pacman 的自动 `-Sy` 后单包安装。普通安装使用已有同步数据库；新增源需要系统同步升级时，在计划中明确完整升级前置条件，不能悄悄执行部分升级或整机升级。
- 未完成某个 manager 的 repository 配置能力时，保留它已验证的包搜索/安装能力，并准确报告 configure unsupported。

### Homebrew / npm / PyPI / Cargo / Go

- Homebrew tap 进入统一目录，formula/cask 的精确解析读取真实 tap 身份；保留现有 tap 分组与安装行为。
- npm/PyPI 的逻辑 registry 身份与 mirror endpoint 分开；查询、精确解析和安装遵守同一绑定。私有或第三方 registry 按实际 API 能力接入，不假设都有全文搜索或官方 JSON API。
- Homebrew 元数据刷新可能影响全部 taps，计划需展示实际影响范围，不能声称只刷新一个 tap。
- Cargo/Go 保留 discovery-only 能力，不因为来源进入目录就宣称已有 managed include 或安装后端。pkg.go.dev 的发现功能与 Go 下载代理继续区分。
- 不在此改造中默认改变 Homebrew、npm、uv 的升级与移除策略；manifest 只声明实际已经执行的能力。

### URL artifact

- 来源目录可声明固定公开安装文件及其 format、平台、架构、原生包身份和 hash/签名信息，身份验证后允许 managed add。
- `.deb` 先接入，随后增加 `.rpm` 对应后端；无对应安装后端的格式报告 unsupported。
- 根据下载后的包元数据核验包名、版本和架构，不能仅以 URL 文件名作为身份。
- `latest` URL 不作为版本 pin；只有可验证的固定版本/hash 才声明可复现性。
- 若厂商安装包会创建软件源，目录描述其 bootstrap 行为，并在安装后重新发现、核对来源。已纳管的 repository 不由 artifact 脚本静默重复接管。
- 来源/软件移出策略不等于卸载已有系统包。原生包升级、卸载和 artifact 更新能力按后端逐项实现，先保证状态与能力报告准确。

## 7. CLI 与前端

以下命令已接入 CLI；各来源的能力与当前主机的 ready 状态决定可执行范围：

```bash
envy sw source ls --json
envy sw source show nix:nixpkgs --json
envy sw source search docker --ecosystem native
envy sw source add native:docker --dry-run --json
envy sw source rm native:docker --dry-run --json
envy sw source sync native:docker --dry-run --json
envy sw se docker-ce --source native --source-id native:docker
envy sw add native.system.package docker-ce --source-id native:docker --dry-run --json
envy sw se hello --source nix --source-id nix:nixpkgs
envy sw add nix.user.package 'flake:pi#default' --dry-run --json
```

`source add/rm` 根据来源类型路由到机器选择或共享项目登记；预览明确列出修改文件、作用范围、来源依赖、索引刷新和待应用操作。`--yes` 与既有 JSON 事务约定一致。

`source sync` 表示元数据同步，不自动修改 Nix 锁定版本。Nix input 更新继续通过现有 `envy update inputs` 工作流；source 命令可以提供入口，但必须复用同一个事务和全机器验证。

已知来源的软件添加可在一个预览中包含 repository 前置选择。显式排除的来源、无法验证的候选或其他机器依赖会阻止计划；不自动消除用户来源排除项。

JSON 错误区分包不存在、来源不可用、来源未就绪、能力不支持、身份歧义、来源被排除、来源仍被依赖与共享验证失败。网络故障不会变成 not-found 或负向缓存；依赖或身份失败不会产生部分策略写入。

TUI 在现有 Configure/Software 与 Search 内展示具体来源及状态，并提供来源管理入口。搜索结果显示可安装、仅发现、需要来源、当前平台不支持等事实。TUI 仍只调用 CLI JSON，不运行 package manager 或直接修改文件。

Setup 读取统一目录和能力，仅编辑机器选择；涉及共享 Nix input 的操作进入独立的共享事务流程，不混入现有机器限定的 commit 提议。

## 8. 分阶段执行

每一阶段单独形成可审查的改动，并完成自身验收后继续。先建立兼容读取，再切换写入；优先形成 Nix 与 Ubuntu APT 的完整流程。

| 阶段 | 具体交付 | 主要代码边界 | 完成条件 |
|---|---|---|---|
| 0：修正已复现行为 | Native exact name/kind 校验、not-found 分类、独立解析器与 locale；Pacman 同步策略修正 | `search/providers.py`、`modules/libs/sys.nix`、provider tests | 无效身份不进入索引/机器策略；正确解析各后端输出 |
| 1：来源契约 | Source schema、manifest v3、v2/v3 reader、来源限定索引与缓存；现有默认来源绑定 | `modules/envy/{source-options,manifest,home,darwin,linux}.nix`、`sources/`、`search/{model,index,runner}.py`、evaluation | 旧机器文件可求值；不同来源同名结果互不污染 |
| 2：锁定的 Nix 来源 | 基于项目 inputs 的搜索/解析/include、第三方 flake、共享来源登记事务 | `flake.nix`、Nix source adapter、software writer/parser、`workflows/update.py` | 搜索/解析/安装 revision 一致；共享变更失败完整回滚 |
| 3：APT 完整流程 | 有效来源集合、native repository schema/backend、来源 CLI、只读索引解析、包/来源联合计划 | `sources/native/`、`modules/software/linux/`、`modules/mirrors/linux.nix`、sys、software planner | china 模式保留厂商候选；依赖顺序正确；dry-run 不写系统配置 |
| 4：其余现有来源与前端 | Homebrew/npm/PyPI 来源绑定、其他 native manager 的仓库能力、Cargo/Go discovery 状态；TUI/setup/doctor 展示 | provider adapters、业务来源贡献、Rust model/backend/ui、setup、doctor、schemas | 前端只用 JSON；后端能力如实报告；各平台边界有效 |
| 5：安装文件与迁移收尾 | `.deb`/`.rpm` 来源添加、身份校验、bootstrap 观测；既有 vendor 安装脚本逐项迁移；文档与旧接口清理 | artifact backend、拥有软件的业务模块、docs | 安装文件验证与 source binding 正确；既有配置不被静默接管 |

阶段 0 可以独立落地。阶段 1 的兼容层必须先于其他阶段的 sourceId 写入。阶段 2 和 3 共用契约，分别完成后再扩大覆盖。

## 9. 验收与验证

必须覆盖的真实行为：

1. 本机 registry 的 nixpkgs 与项目锁定版本不同，envY 仍按项目版本搜索、解析和构建。
2. 同名包来自两个来源，结果和 index 独立；添加需要明确选择，旧 stable exclusions 保持有效。
3. 第三方 flake 输出仅支持一平台时，另一平台正确显示不支持；没有 package 输出的 input 不进入软件包来源列表。
4. Ubuntu china 模式下，发行版包与已存在的厂商源都能得到一致候选；source set 与安装使用的配置一致。
5. 添加已知厂商包的计划包含未配置来源；来源已被显式排除或索引签名/身份验证失败时，全部机器文件保持原样。
6. 移除仍被有效软件依赖的来源被拒绝；移除共享 Nix 来源检查所有机器与模块依赖。
7. 多文件保存、Nix input 登记或锁更新失败时回滚；系统 apply 的部分成功明确记录并可重跑，不声称 APT/RPM 数据库变更可以被文件事务完整撤销。
8. 切换 mirror 不改来源身份；改变锁、架构、发行版、仓库或信任信息使旧缓存失效。Offline 不接受来源不匹配的旧索引。
9. `search`、`show`、completion 和 TUI 浏览不需要 sudo，不写系统来源，也不隐式更新 lock；显式 sync/apply 才执行对应系统操作。
10. 手写 sources、host 配置、现有登录状态和所有秘密不被接管或输出。Kernel headers resolver 与 `names.<manager>` 保持兼容。

验证方式：

- Python provider fixtures 使用实际输出与失败条件；APT 覆盖多个版本、无候选、virtual/模糊包名、同名多源和签名异常；DNF4/5、Zypper、Pacman 独立覆盖。
- Planner/CLI 验证来源依赖、显式排除、scope、dry-run/JSON、离线行为与多文件回滚。
- Nix checks 验证 manifest v3、source/reference 完整性、跨平台输出与所有主机求值；使用小型本地 flake fixture 验证锁定来源，无需构建大型应用。
- 使用临时 APT repository 和隔离容器/VM 验证 source set、keyring、候选与安装；其他 native manager 使用对应隔离系统验证，不在用户机器上进行破坏性测试。
- Rust 验证 v2/v3/新增来源字段读取、依赖预览和失败后的缓存刷新。更新 docs/software、mirrors、machines、tui 与 AGENTS 中已经实施的接口说明。

## 10. 第一轮边界

第一轮完成统一来源契约，并走通锁定 Nix、Ubuntu APT、Homebrew 与现有 npm/PyPI 的来源绑定。其余后端按阶段补齐；能力尚未完成时保持显式 unsupported/discovery-only。

来源目录不承诺从任意软件名自动发现互联网上未知的厂商仓库。新增仓库通过维护的官方定义或用户的结构化配置接入。Flatpak、Snap、AUR 等在出现明确需求后增加独立生态适配器，不混入普通 native 仓库包。

本计划的主要完成标准是：软件来源可解释、选择可审查，搜索与安装一致，既有机器配置可迁移。系统包的全自动升级、卸载与完整回滚需要额外的所有权和后端能力设计，不作为来源层已实现的默认承诺。

## 11. 实施记录

六个阶段已落到当前工作区。source/catalog 与 source-specific adapter 分离，
manifest 输出 v3，读取端兼容 v2/v3，CLI JSON envelope 保持 v1。

| 阶段 | 实际交付 |
|---|---|
| 0 | Native 精确包名与 multiarch 核验，not-found/unavailable 区分，DNF4/5、Zypper、Pacman 解析器；取消自动 pacman -Sy |
| 1 | 通用来源 schema、声明与观测状态、来源限定 v2 SQLite 索引、上下文/query cache、具体来源与 resolvedVersion |
| 2 | 项目锁定包集合与 legacyPackages 搜索入口，nixpkgs/Pi/OMP 登记、wrapper 上游依赖；第三方输入登记/删除、锁定与全机器事务验证 |
| 3 | 共用 APT effective source set、固定指纹 keyring/repository、私有签名索引、来源限定内容安装、来源/软件联合计划、source CLI |
| 4 | 实际 Homebrew tap 绑定与 revision、npm/PyPI 绑定与 endpoint 参数、DNF/Zypper 自有 repo 后端、诚实的 Pacman/Cargo/Go 能力；TUI、Setup 与 Doctor |
| 5 | hash + 原生身份验证的 deb/rpm managed add、激活时重复核验、bootstrap 来源观测；Docker/Waydroid 脚本迁移与文档 |

明确的能力边界：首批 Docker/Waydroid 厂商定义支持 Ubuntu/Debian APT。
DNF/Zypper 的来源限定包查询/安装与私有元数据同步、Pacman 仓库配置、
Cargo/Go managed install 均返回 unsupported/discovery-only，符合分阶段设计的
能力约束。Native 与 artifact 仍按首次安装模型声明能力；取消选择不等于
卸载系统包，不自动进行整机升级。自定义来源定义和共享 input 的登记使用
CLI；TUI 可管理已有 repository 选择并同步元数据。

为验证来源的真实接线，除单元测试外，还执行了项目锁定 Nix 包的实际
search/exact/dry-run、共享 input 登记 dry-run、Linux activation derivation
求值以及跨平台 schema/策略检查。新增可重复执行的 CLI/Nix E2E，以及
一次性 Debian 13 / aarch64 容器中的真实签名仓库、来源限定包安装和
artifact 安装验证；安装入口也在禁用 Python site-packages 的子进程中
验证。具体流程与复现命令见 [Software source E2E](software-sources-e2e.md)。
用户主机未执行系统激活或包安装。
