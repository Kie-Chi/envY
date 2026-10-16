Pi 专属规则：

## 工具编排与 codemode

`codemode` 是仓库和编码任务的默认工具编排入口。它可以在沙箱 JavaScript
中通过 `tools.<name>(args)` 调用 Pi 的其他工具，并只把脚本整理后的结果返回给你。

先按下面的规则选择调用方式：

- 纯解释、问答或写作任务，不需要访问工作区时，不调用工具。
- envY 默认使用 `codemode.mode = "only"`：只要任务需要访问工作区、读取文件、运行命令或修改内容，就先调用 `codemode`，包括只有一个简单操作的情况。只有机器显式改回 `codemode.mode = "on"`，或 codemode 不可用时，才可以直接调用对应工具。
- 无论当前模式如何，只要任务需要检查仓库、搜索文件、读取多个文件、并行独立操作、读后修改、修改后验证、运行多个命令、整理较大输出，或包含两个及以上工具步骤，就必须使用 `codemode`。

对于后一类任务，第一次工具调用就应该是 `codemode`。不要先分别调用
`read`、`bash`、`grep`、`find`、`edit` 或 `write`，之后才决定是否使用
codemode。把同一阶段的独立只读调用放进一个脚本，用
`Promise.allSettled([...])` 并行执行并逐项检查结果；编辑、验证、审批和依赖前一步
结果的调用按顺序执行。搜索结果需要你判断下一步时，先结束脚本检查结果，再编排
下一阶段，不要在一个脚本中猜测整条修改流程。只输出完成下一步所需的文件路径、
相关内容、匹配项和错误，避免把完整的大文件或冗长命令输出原样返回给模型。

`Script completed` 只说明脚本执行结束，不代表所有内部调用都成功：必须检查
`Promise.allSettled` 的拒绝项、`bash` 返回的 `exit_code` 和 MCP 返回的
`isError`，保留失败诊断；测试失败时不得报告验证通过。JavaScript 沙箱没有
Node.js、文件系统或网络 API，读写和命令执行都通过 `tools` 完成。对尚未列出的
工具，先用 `searchTools()` 和 `describeTool()` 获取实际名称与参数，再调用。

常用模式如下（传给 `codemode` 的内容是原始 JavaScript，不要包 Markdown 代码围栏）：

```js
const results = await Promise.allSettled([
  tools.bash({ command: "git status --short" }),
  tools.read({ path: "AGENTS.md" }),
  tools.read({ path: "package.json" }),
]);
return results.map((result) =>
  result.status === "fulfilled" ? result.value : { error: String(result.reason) },
);
```

修改代码时，先用 codemode 完成约定、相关文件和状态检查，再进行编辑；编辑完成后
继续用 codemode 运行合适的测试、类型检查、格式检查或构建，并根据失败结果修复后
重跑。脚本失败时先修正脚本或参数；只有 codemode 不可用，或操作确实不适合脚本
编排时，才退回直接工具调用。不要在 codemode 脚本中再次调用 codemode。

## 执行规则

- 在执行修改前，先检查项目约定、相关文件和 git 状态；完成后运行适合的验证。
- 直接使用工具完成任务，不要只给出本可以执行的命令或建议。
