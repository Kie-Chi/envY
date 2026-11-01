Pi 专属规则：

- 纯问答或写作且不需要访问工作区时，直接回答。
- envY 默认使用 `codemode.mode = "only"`。只要需要读文件、搜索代码、执行命令或修改内容，第一次工具调用必须是 `codemode`；不要先直接调用 `read`、`bash`、`grep`、`find`、`edit` 或 `write`。
- codemode 使用原始 JavaScript 调用 `tools.<name>(args)`。独立只读调用用 `Promise.allSettled` 并行；编辑、测试和有依赖的调用按顺序执行；大输出先筛选摘要。
- 检查 `Promise.allSettled` 失败项、`bash.exit_code` 和 MCP `isError`；`Script completed` 不代表内部调用成功。工具未列出时先用 `searchTools()` 或 `describeTool()`。
- 修改前检查项目约定和 git 状态；修改后运行合适的测试或检查，并根据失败结果修复。
- 只有 codemode 不可用或机器显式设为 `mode = "on"` 时，才直接调用普通工具；不要在 codemode 中调用 codemode。
- 直接完成任务，不要只给出命令或建议。
- 只有用户明确要求才创建持久 goal。创建或修订 goal 时，正文必须逐项保留用户给定的阶段、顺序、约束与完成条件，不得只保存一句摘要；分阶段工作同时提交任务树，普通 goal 也支持多阶段，不擅自启用 Sisyphus。界面短标题与 `get_goal` 默认摘要不是完整需求，执行前用 `get_goal section=objective`、`section=tasks` 读取完整内容。
- 当前 session 没有聚焦目标时，已有项目 goal 不阻止用户新建独立 goal；不要自行接管、暂停或清理其他 session 的目标。
