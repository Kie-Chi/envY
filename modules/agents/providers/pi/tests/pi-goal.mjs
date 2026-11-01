import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { fileURLToPath } from "node:url";
import { test, after } from "node:test";

// Load the real extension with the host Pi modules, without model requests.
const [extensionPath, nodeModules] = process.argv.slice(2);
assert.ok(extensionPath && nodeModules, "Usage: node pi-goal.mjs <goal package> <Pi node_modules>");
const extensionRoot = fs.realpathSync(extensionPath);
const require = createRequire(path.join(nodeModules, "jiti/package.json"));
const { createJiti } = require("jiti");
const aliases = Object.fromEntries(["pi-ai", "pi-coding-agent", "pi-tui", "pi-agent-core"].map(name => [
  "@earendil-works/" + name, path.join(nodeModules, "@earendil-works/" + name + "/dist/index.js"),
]));
const jiti = createJiti(fileURLToPath(import.meta.url), { alias: aliases, moduleCache: true, fsCache: false });
// Jiti snapshots native module functions; install the fault boundary before loading.
const blockedUnlinks = new Set();
const blockedReads = new Set();
const originalRead = fs.readFileSync;
fs.readFileSync = (file, ...args) => {
  if (typeof file === "string" && blockedReads.has(path.resolve(file))) {
    throw Object.assign(new Error("Injected goal read failure"), { code: "EACCES" });
  }
  return originalRead(file, ...args);
};
const originalUnlink = fs.unlinkSync;
let unlinkFailures = 0;
fs.unlinkSync = (file, ...args) => {
  if (blockedUnlinks.has(path.resolve(file))) {
    unlinkFailures++;
    throw Object.assign(new Error("Injected active unlink failure"), { code: "EACCES" });
  }
  return originalUnlink(file, ...args);
};
syncBuiltinESMExports();
const goalExtension = (await jiti.import(path.join(extensionRoot, "extensions/goal.ts"))).default;
const files = await jiti.import(path.join(extensionRoot, "extensions/storage/goal-files.ts"));
const { createGoal } = await jiti.import(path.join(extensionRoot, "extensions/goal-record.ts"));
const { acquireGoalLock } = await jiti.import(path.join(extensionRoot, "extensions/storage/goal-lock.ts"));
const { goalDraftingPrompt } = await jiti.import(path.join(extensionRoot, "extensions/goal-draft.ts"));
const settings = await jiti.import(path.join(extensionRoot, "extensions/goal-settings.ts"));

const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-goal-"));
process.env.PI_CODING_AGENT_DIR = path.join(root, "agent");
process.env.PI_GOAL_GLOBAL_SETTINGS_FILE = path.join(root, "agent/pi-goal-x-settings.json");
process.env.PI_GOAL_AUTO_CONFIRM = "1";
// Keep every fixture independent of the invoking user's settings and state.
for (const key of Object.keys(process.env)) {
  if (key.startsWith("PI_GOAL_") && !["PI_GOAL_GLOBAL_SETTINGS_FILE", "PI_GOAL_AUTO_CONFIRM"].includes(key)) delete process.env[key];
}
delete process.env.PI_SUBAGENT_DEPTH;
fs.mkdirSync(process.env.PI_CODING_AGENT_DIR, { recursive: true });
fs.writeFileSync(process.env.PI_GOAL_GLOBAL_SETTINGS_FILE, JSON.stringify({
  hideUnfocusedBanner: true, hideUnfocusedPrompt: true, autoSelectSingleGoal: false,
}));
after(() => {
  fs.unlinkSync = originalUnlink; fs.readFileSync = originalRead; syncBuiltinESMExports();
  fs.rmSync(root, { recursive: true, force: true });
});
let fixtureId = 0;

async function harness(cwd = path.join(root, "project-" + ++fixtureId), entries = []) {
  fs.mkdirSync(cwd, { recursive: true });
  files.invalidateGoalPoolCache();
  settings.invalidateGoalSettingsCache();
  const tools = new Map();
  const handlers = new Map();
  const notifications = [];
  const widgets = [];
  const sessionId = "session-" + ++fixtureId;
  const api = {
    registerTool: tool => tools.set(tool.name, tool),
    registerCommand() {}, registerShortcut() {}, registerMessageRenderer() {},
    on: (name, handler) => {
      const list = handlers.get(name) ?? [];
      list.push(handler); handlers.set(name, list); return () => {};
    },
    getActiveTools: () => [...tools.keys()],
    getAllTools: () => [...tools.values()],
    setActiveTools() {}, appendEntry: (customType, data) => entries.push({ type: "custom", customType, data }),
    sendMessage() {}, sendUserMessage() {},
  };
  const ctx = {
    cwd, hasUI: true, mode: "rpc", isIdle: () => true,
    abort() {}, getContextUsage: () => undefined,
    sessionManager: { getSessionId: () => sessionId, getBranch: () => entries, getEntries: () => entries },
    ui: {
      notify: (message, type) => notifications.push({ message, type }),
      confirm: async () => true,
      select: async (_title, options) => options[0],
      setStatus() {}, setWidget: (key, value) => widgets.push({ key, value }),
      onTerminalInput: () => () => {}, setWorkingVisible() {},
    },
  };
  goalExtension(api);
  const core = api._goalCore;
  await core.loadState(ctx);
  // UI throttling is irrelevant to state tests; exercise rendering directly.
  core.updateUI = () => {};
  const call = (name, args = {}) => tools.get(name).execute("test-call", args, undefined, undefined, ctx);
  const clear = () => core.invokeGoalCommand("goal-clear", "", ctx);
  const seed = () => {
    const goal = files.writeActiveGoalFile(ctx, { ...createGoal({ objective: "Fixture objective", autoContinue: false }), revision: 10 });
    core.state.goal = goal;
    core.reconcileFocusedGoalFromDisk(ctx);
    assert.equal(core.state.goal?.id, goal.id, "Seed must have authoritative focus");
    return goal;
  };
  const disk = goal => files.parseGoalFile(path.join(cwd, goal.activePath));
  const active = goal => fs.existsSync(path.join(cwd, goal.activePath));
  const archived = () => fs.existsSync(path.join(cwd, ".pi/goals/archived"))
    ? fs.readdirSync(path.join(cwd, ".pi/goals/archived")).filter(name => name.endsWith(".md")) : [];
  const externalEdit = goal => {
    const changed = { ...disk(goal), revision: (disk(goal).revision ?? 0) + 1, objective: "Externally updated objective" };
    // Deliberately bypass the extension's process-local cache invalidation.
    fs.writeFileSync(path.join(cwd, goal.activePath), files.serializeGoalFile(changed));
    return changed;
  };
  const success = () => notifications.some(note => note.message === "Goal cleared and archived.");
  return { core, ctx, call, clear, seed, disk, active, archived, externalEdit, notifications, widgets, handlers, entries, success };
}

for (const stale of [false, true]) {
  test(stale ? "clear rereads stale pool and persistent snapshot" : "clear archives before dropping focus", async () => {
    const h = await harness(); const goal = h.seed();
    if (stale) h.externalEdit(goal);
    await h.clear();
    assert.equal(h.active(goal), false);
    assert.equal(h.archived().length, 1);
    assert.equal(h.core.focusedGoalId, null);
    assert.equal(h.success(), true);
    const saved = files.parseGoalFile(path.join(h.ctx.cwd, ".pi/goals/archived", h.archived()[0]));
    assert.equal(saved.objective, stale ? "Externally updated objective" : goal.objective);
    files.invalidateGoalPoolCache(); await h.core.loadState(h.ctx);
    assert.equal(h.core.focusedGoalId, null);
  });
}

test("cancel does not change files or focus", async () => {
  const h = await harness(); const goal = h.seed();
  const before = fs.readFileSync(path.join(h.ctx.cwd, goal.activePath), "utf8");
  const entries = h.entries.length;
  h.ctx.ui.confirm = async () => false;
  await h.clear();
  assert.equal(fs.readFileSync(path.join(h.ctx.cwd, goal.activePath), "utf8"), before);
  assert.equal(h.core.focusedGoalId, goal.id);
  assert.equal(h.entries.length, entries);
  assert.deepEqual(h.archived(), []);
});

test("headless clear requires explicit interactive confirmation", async () => {
  const h = await harness(); const goal = h.seed(); h.ctx.hasUI = false;
  await h.clear(); assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id);
});

for (const kind of ["revision", "focus"]) {
  test("clear rejects confirmation-time " + kind + " changes", async () => {
    const h = await harness(); const goal = h.seed();
    h.ctx.ui.confirm = async () => {
      if (kind === "revision") h.externalEdit(goal);
      else {
        h.core.assignFocusedGoalId(null);
        h.core.assignFocusedGoalId(goal.id); // Even an away-and-back switch invalidates confirmation.
      }
      return true;
    };
    await h.clear();
    assert.equal(h.active(goal), true); assert.equal(h.success(), false); assert.deepEqual(h.archived(), []);
    assert.equal(h.core.focusedGoalId, goal.id);
  });
}

test("unreadable goal files preserve focus and report a storage error", async () => {
  const h = await harness(); const goal = h.seed();
  const activePath = fs.realpathSync(path.join(h.ctx.cwd, goal.activePath));
  const requestedPath = path.resolve(h.ctx.cwd, goal.activePath);
  blockedReads.add(activePath); blockedReads.add(requestedPath);
  try { await h.clear(); } finally { blockedReads.delete(activePath); blockedReads.delete(requestedPath); }
  assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id);
  assert.equal(h.success(), false);
  assert.ok(h.notifications.some(note => /read failure/i.test(note.message) && note.type === "error"));
});

test("locked revision check reads contents even when size and mtime are unchanged", async () => {
  const h = await harness(); const goal = h.seed();
  const activePath = path.join(h.ctx.cwd, goal.activePath);
  const fixedTime = new Date("2026-01-01T00:00:00.000Z");
  fs.utimesSync(activePath, fixedTime, fixedTime);
  h.ctx.ui.confirm = async () => {
    const before = fs.statSync(activePath);
    const text = fs.readFileSync(activePath, "utf8").replace('"revision": 10', '"revision": 11');
    fs.writeFileSync(activePath, text);
    fs.utimesSync(activePath, before.atime, before.mtime);
    assert.equal(fs.statSync(activePath).size, before.size);
    assert.equal(fs.statSync(activePath).mtimeMs, before.mtimeMs);
    return true;
  };
  await h.clear();
  assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id); assert.equal(h.success(), false);
  assert.ok(h.notifications.some(note => /revision.*11/i.test(note.message)));
});

test("lock failure preserves focus and reports the cause", async () => {
  const h = await harness(); const goal = h.seed();
  const lock = acquireGoalLock(h.ctx, goal.id);
  try { await h.clear(); } finally { lock.release(); }
  assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id);
  assert.equal(h.success(), false);
  assert.ok(h.notifications.some(note => /lock/i.test(note.message) && note.type === "error"));
});

test("archive write failure preserves focus and reports failure", async () => {
  const h = await harness(); const goal = h.seed();
  fs.writeFileSync(path.join(h.ctx.cwd, ".pi/goals/archived"), "not a directory");
  await h.clear();
  assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id); assert.equal(h.success(), false);
  assert.ok(h.notifications.some(note => note.type === "error"));
});

test("active unlink failure must not be swallowed after writing an archive", async () => {
  const h = await harness(); const goal = h.seed();
  const activePath = fs.realpathSync(path.join(h.ctx.cwd, goal.activePath));
  const requestedPath = path.resolve(h.ctx.cwd, goal.activePath);
  const failures = unlinkFailures;
  blockedUnlinks.add(activePath); blockedUnlinks.add(requestedPath);
  try { await h.clear(); } finally { blockedUnlinks.delete(activePath); blockedUnlinks.delete(requestedPath); }
  assert.ok(unlinkFailures > failures, "The unlink fault must actually fire");
  assert.equal(h.active(goal), true); assert.equal(h.core.focusedGoalId, goal.id); assert.equal(h.success(), false);
  assert.ok(h.notifications.some(note => /unlink/i.test(note.message) && note.type === "error"));
});

test("clear flushes buffered changes before asking for confirmation", async () => {
  const h = await harness(); const goal = h.seed();
  h.core.goalService.beginTurn(h.ctx, goal.id);
  assert.equal(h.core.goalService.apply(h.ctx, { mutate: g => ({ ...g, objective: "Buffered objective" }) }).ok, true);
  h.ctx.ui.confirm = async () => { assert.equal(h.disk(goal).objective, "Buffered objective"); return true; };
  await h.clear(); assert.equal(h.active(goal), false); assert.equal(h.success(), true);
});

test("failed buffer flush does not clear focus or discard pending writes", async () => {
  const h = await harness(); const goal = h.seed();
  h.core.goalService.beginTurn(h.ctx, goal.id);
  h.core.goalService.apply(h.ctx, { mutate: g => ({ ...g, objective: "Pending objective" }) });
  let asked = false; h.ctx.ui.confirm = async () => { asked = true; return true; };
  const lock = acquireGoalLock(h.ctx, goal.id);
  try { await h.clear(); } finally { lock.release(); }
  assert.equal(asked, false); assert.equal(h.core.focusedGoalId, goal.id); assert.equal(h.success(), false);
  assert.equal(h.core.goalService.isTurnBuffered(), true);
  h.core.goalService.endTurn(h.ctx);
  assert.equal(h.disk(goal).objective, "Pending objective");
});

test("clear can retry after a conflicting buffered transaction is rejected", async () => {
  const h = await harness(); const goal = h.seed();
  h.core.goalService.beginTurn(h.ctx, goal.id);
  h.core.goalService.apply(h.ctx, { mutate: g => ({ ...g, objective: "Rejected pending plan" }) });
  h.externalEdit(goal);
  await h.clear();
  assert.equal(h.success(), false); assert.equal(h.active(goal), true);
  assert.equal(h.core.focusedGoalId, goal.id);
  await h.clear();
  assert.equal(h.success(), true); assert.equal(h.active(goal), false);
});

const objective = "Complete the approved pipeline.\n1. Draft the specification; preserve all branches.\n2. Build isolated environments; acceptance tests must pass.\n3. Run the experiments, retain evidence and analyze results.\nConstraints: do not commit or replace existing data.";

test("direct creation saves full numbered objective and stage tasks atomically", async () => {
  const h = await harness(); await h.call("create_goal", { objective });
  const goal = h.core.state.goal;
  assert.equal(goal.objective, objective);
  assert.deepEqual(goal.taskList.tasks.map(task => task.title), objective.split("\n").slice(1, 4).map(line => line.slice(3)));
  assert.equal(h.disk(goal).taskList.tasks.length, 3);
  files.invalidateGoalPoolCache(); await h.core.loadState(h.ctx);
  assert.equal(h.core.state.goal.objective, objective); assert.equal(h.core.state.goal.taskList.tasks.length, 3);
  const detail = await h.call("get_goal", { section: "objective" });
  assert.ok(detail.content.some(item => item.text?.includes(objective)));
});

test("explicit parent-linked stages and verification criteria persist together", async () => {
  const h = await harness();
  await h.call("create_goal", { objective: "Complete the approved staged plan without changing its scope.", tasks: [
    { id: "build", title: "Build the approved environment" },
    { id: "accept", title: "Run acceptance tests", parent_id: "build", verification_contract: "All acceptance tests pass" },
    { id: "analyze", title: "Analyze the recorded experiments" },
  ] });
  const goal = h.core.state.goal;
  assert.equal(goal.taskList.tasks[0].subtasks[0].verificationContract, "All acceptance tests pass");
  assert.deepEqual(JSON.parse(JSON.stringify(h.disk(goal).taskList)), JSON.parse(JSON.stringify(goal.taskList)));
});

test("invalid stage tree leaves the previous goal and its focus unchanged", async () => {
  const h = await harness(); const previous = h.seed();
  await assert.rejects(h.call("create_goal", { objective: "New goal", tasks: [{ id: "child", title: "Child", parent_id: "missing" }] }), /missing parent/i);
  assert.equal(h.core.focusedGoalId, previous.id);
  assert.equal(files.readActiveGoalPool(h.ctx).size, 1);
});

test("Chinese phase markers remain ordered tasks", async () => {
  const h = await harness(); const phases = "完成已批准计划。\n阶段一：细化方案并审核。\n阶段二：构建环境并完整验收。\n阶段三：运行实验并分析证据。";
  await h.call("create_goal", { objective: phases });
  assert.equal(h.core.state.goal.objective, phases); assert.equal(h.core.state.goal.taskList.tasks.length, 3);
});

test("guided normal creation preserves the complete staged objective and tasks", async () => {
  const h = await harness();
  await h.core.invokeGoalCommand("goal", objective, h.ctx);
  await h.call("propose_goal_draft", { objective, sisyphus: false });
  assert.equal(h.core.state.goal.objective, objective);
  assert.equal(h.core.state.goal.taskList.tasks.length, 3);
  assert.equal(h.disk(h.core.state.goal).taskList.tasks.length, 3);
});

test("guided drafting rejects a summary that drops user-provided stages", async () => {
  const h = await harness();
  await h.core.invokeGoalCommand("goal", objective, h.ctx);
  await assert.rejects(h.call("propose_goal_draft", { objective: "Complete the research pipeline." }), /3 ordered stages/i);
  assert.equal(h.core.state.goal, null);
  assert.equal(files.readActiveGoalPool(h.ctx).size, 0);
});

test("new creation flushes the previous goal without archiving or overwriting it", async () => {
  const h = await harness(); const previous = h.seed();
  h.core.goalService.beginTurn(h.ctx, previous.id);
  h.core.goalService.apply(h.ctx, { mutate: g => ({ ...g, objective: "Previous goal's pending work" }) });
  await h.call("create_goal", { objective: "A separate requested goal." });
  assert.equal(h.disk(previous).objective, "Previous goal's pending work");
  assert.notEqual(h.core.focusedGoalId, previous.id);
  assert.equal(files.readActiveGoalPool(h.ctx).size, 2);
});

test("task-disabled settings retain the full objective without enabling tasks", async () => {
  const h = await harness();
  fs.mkdirSync(path.join(h.ctx.cwd, ".pi"), { recursive: true });
  fs.writeFileSync(path.join(h.ctx.cwd, ".pi/pi-goal-x-settings.json"), JSON.stringify({ disableTasks: true }));
  settings.invalidateGoalSettingsCache(); await h.core.loadState(h.ctx);
  await h.call("create_goal", { objective });
  assert.equal(h.core.state.goal.objective, objective); assert.equal(h.core.state.goal.taskList, undefined);
});

test("single-step goals do not acquire an invented task plan", async () => {
  const h = await harness(); await h.call("create_goal", { objective: "Check the existing deployment status." });
  assert.equal(h.core.state.goal.taskList, undefined);
});

test("single-checklist guided work does not require an invented multi-stage structure", async () => {
  const h = await harness();
  await h.core.invokeGoalCommand("goal", "- [ ] Check the existing deployment status.", h.ctx);
  await h.call("propose_goal_draft", { objective: "Check the existing deployment status." });
  assert.equal(h.core.state.goal.objective, "Check the existing deployment status.");
  assert.equal(h.core.state.goal.taskList, undefined);
});

test("ordinary guided goal prompt preserves stages rather than demanding one sentence", () => {
  const prompt = goalDraftingPrompt(objective, "goal");
  assert.ok(prompt.includes("Ordered steps:")); assert.equal(prompt.includes("one-sentence outcome"), false);
});

test("two sessions create independent goals without adopting or mutating the other", async () => {
  const a = await harness(); await a.call("create_goal", { objective });
  const first = a.core.state.goal; const before = fs.readFileSync(path.join(a.ctx.cwd, first.activePath), "utf8");
  const b = await harness(a.ctx.cwd);
  assert.equal(b.core.focusedGoalId, null);
  await b.call("create_goal", { objective: "Complete a separate user-requested goal." });
  const second = b.core.state.goal;
  assert.notEqual(first.id, second.id); assert.equal(a.core.focusedGoalId, first.id);
  assert.equal(fs.readFileSync(path.join(a.ctx.cwd, first.activePath), "utf8"), before);
  assert.equal(files.readActiveGoalPool(a.ctx).size, 2);
  files.invalidateGoalPoolCache(); await a.core.loadState(a.ctx);
  files.invalidateGoalPoolCache(); await b.core.loadState(b.ctx);
  assert.equal(a.core.focusedGoalId, first.id); assert.equal(b.core.focusedGoalId, second.id);
});

test("unfocused sessions neither inject a focus demand nor display its banner", async () => {
  const a = await harness(); await a.call("create_goal", { objective });
  const b = await harness(a.ctx.cwd);
  for (const handler of b.handlers.get("context") ?? []) {
    const result = await handler({ messages: [] }, b.ctx);
    assert.equal(JSON.stringify(result ?? {}).includes("PI GOAL UNFOCUSED"), false);
  }
  assert.equal(b.widgets.some(widget => widget.value !== undefined), false);
});
