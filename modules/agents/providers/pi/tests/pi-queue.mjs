import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [extensionPath, nodeModules] = process.argv.slice(2);
assert.ok(extensionPath && nodeModules, "Usage: node pi-queue.mjs <queue package> <Pi node_modules>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-queue-test-"));
const agentDir = path.join(root, "agent");
process.env.PI_CODING_AGENT_DIR = agentDir;
process.env.HOME = path.join(root, "home");
process.env.PI_OFFLINE = "1";
fs.mkdirSync(process.env.HOME);
fs.mkdirSync(path.join(agentDir, "extensions"), { recursive: true });
fs.symlinkSync(fs.realpathSync(extensionPath), path.join(agentDir, "extensions/envy-queue-steer"));
fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({
  steeringMode: "one-at-a-time", followUpMode: "one-at-a-time",
}));
fs.writeFileSync(path.join(agentDir, "keybindings.json"), JSON.stringify({
  "tui.input.submit": "enter", "app.message.followUp": "alt+enter", "app.message.dequeue": "alt+up",
}));
// These tests load the real host and editor but never contact a model.
globalThis.fetch = () => { throw new Error("Network access is forbidden in queue tests"); };
const hostPath = path.join(nodeModules, "@earendil-works/pi-coding-agent/dist");
const host = await import(pathToFileURL(path.join(hostPath, "index.js")));
const themes = await import(pathToFileURL(path.join(hostPath, "modes/interactive/theme/theme.js")));
const { KeybindingsManager } = await import(pathToFileURL(path.join(hostPath, "core/keybindings.js")));
const tui = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-tui/dist/index.js")));
host.initTheme("dark", false);
const keybindings = KeybindingsManager.create(agentDir);
tui.setKeybindings(keybindings);
after(() => fs.rmSync(root, { recursive: true, force: true }));

const keys = {
  enter: "\r", followUp: "\x1b\r", previous: "\x1b[1;3A", next: "\x1b[1;3B",
  up: "\x1b[1;4A", down: "\x1b[1;4B", remove: "\x1bx", toggle: "\x1bt", hold: "\x1b[112;3u", cancel: "\x1b",
};
const assistant = {
  role: "assistant", content: [], stopReason: "stop",
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
let fixtureId = 0;

async function harness(t, mode = "tui", initialEntries = []) {
  const cwd = path.join(root, "project-" + ++fixtureId);
  fs.mkdirSync(cwd);
  const loaded = await host.discoverAndLoadExtensions([], cwd, agentDir);
  assert.deepEqual(loaded.errors, [], "The installed directory must load through Pi discovery");
  assert.equal(loaded.extensions.length, 1);
  const extension = loaded.extensions[0];
  const sent = [];
  const entries = structuredClone(initialEntries);
  const notifications = [];
  const confirmations = [];
  const statuses = new Map();
  const statusUpdates = [];
  const widgets = new Map();
  const widgetUpdates = [];
  let confirmation = false;
  let aborts = 0;
  let editor;
  let factory;
  let idle = false;
  let draft = "";
  const terminal = { requestRender() {}, terminal: { rows: 40, columns: 100 } };
  const ctx = {
    cwd, mode, hasUI: mode === "tui" || mode === "rpc", isIdle: () => idle, isProjectTrusted: () => true,
    model: { contextWindow: 200000 },
    sessionManager: { getBranch: () => entries },
    abort() { aborts += 1; idle = true; },
    ui: {
      theme: themes.theme,
      getEditorComponent: () => {
        assert.equal(mode, "tui", "Only TUI may access terminal editors");
        return factory;
      },
      setEditorComponent(value) {
        factory = value;
        editor = value?.(terminal, themes.getEditorTheme(), keybindings);
      },
      getEditorText: () => editor?.getText() ?? draft,
      setEditorText(value) { draft = value; editor?.setText(value); },
      setWidget(key, content) {
        if (key === "queue-control-result") { widgets.set(key, content); return; }
        if (key !== "queue-messages") return;
        assert.equal(mode, "rpc");
        widgetUpdates.push(content);
        if (content === undefined) widgets.delete(key);
        else widgets.set(key, content);
      },
      setStatus(key, text) {
        statusUpdates.push({ key, text });
        if (text === undefined) statuses.delete(key);
        else statuses.set(key, text);
      },
      notify: (message, level) => notifications.push({ message, level }),
      async confirm(title, message) {
        confirmations.push({ title, message });
        return typeof confirmation === "function" ? confirmation() : confirmation;
      },
    },
  };
  loaded.runtime.sendUserMessage = (content, options) => sent.push({ content, options });
  loaded.runtime.appendEntry = (customType, data) => entries.push({ type: "custom", customType, data });
  loaded.runtime.getCommands = () => [...extension.commands.values()].map(command => ({
    name: command.name, source: "extension", sourceInfo: command.sourceInfo,
  }));
  const emit = async (name, event = {}) => {
    let result;
    for (const handler of extension.handlers.get(name) ?? []) {
      result = await handler({ type: name, ...event }, ctx);
    }
    return result;
  };
  await emit("session_start", { reason: "startup" });
  t.after(async () => {
    await emit("session_shutdown", { reason: "exit" });
    loaded.runtime.invalidate();
  });
  const input = (text, streamingBehavior = "followUp") => emit("input", {
    source: "interactive", text, images: [], streamingBehavior,
  });
  const press = key => { assert.ok(editor, "TUI editor must be installed"); editor.handleInput(keys[key]); };
  const snapshot = () => entries.findLast(entry => entry.customType === "pi-queue-steer:queue")?.data;
  return {
    ctx, emit, input, press, sent, entries, notifications, confirmations, snapshot, statuses, statusUpdates, widgetUpdates,
    queueMessages: () => widgets.has("queue-messages") ? JSON.parse(widgets.get("queue-messages")[0]) : undefined,
    controlResult: () => widgets.has("queue-control-result") ? JSON.parse(widgets.get("queue-control-result")[0]) : undefined,
    commands: extension.commands,
    command: (args, name = "queue") => {
      const command = extension.commands.get(name);
      assert.ok(command, "Command must register through real Pi discovery: " + name);
      return command.handler(args, ctx);
    },
    get aborts() { return aborts; },
    setConfirmation(value) { confirmation = value; },
    failSend() { loaded.runtime.sendUserMessage = () => { throw new Error("offline dispatch failure"); }; },
    get editor() { return editor; },
    setIdle(value) { idle = value; },
  };
}

test("managed package loads and delivers one follow-up per completed run", async t => {
  const h = await harness(t);
  assert.deepEqual(await h.input("First task"), { action: "handled" });
  await h.input("Second task");
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent, [], "Follow-ups must wait for the run to finish");
  await h.emit("agent_end", { messages: [assistant] });
  assert.deepEqual(h.sent, [{ content: "First task", options: { deliverAs: "followUp" } }]);
  await h.emit("agent_end", { messages: [assistant] });
  assert.equal(h.sent[1].content, "Second task");
  assert.deepEqual(h.snapshot().rows, []);
});

test("editor saves text, reorder and removal together and restores the composer draft", async t => {
  const h = await harness(t);
  await h.input("Keep task");
  await h.input("Remove task");
  await h.input("Edit task");
  h.editor.setText("Unsent composer draft");
  h.press("previous");
  assert.equal(h.editor.getText(), "Edit task");
  h.editor.setText("Edited task");
  h.press("up");
  h.press("up");
  // Render the real wrapped editor, including narrow terminal layouts.
  for (const width of [30, 100]) assert.ok(h.editor.render(width).length > 0);
  h.press("next");
  assert.equal(h.editor.getText(), "Keep task");
  h.press("next");
  assert.equal(h.editor.getText(), "Remove task");
  h.press("remove");
  h.press("enter");
  assert.equal(h.editor.getText(), "Unsent composer draft");
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Edited task", "Keep task"]);
  await h.emit("agent_end", { messages: [assistant] });
  await h.emit("agent_end", { messages: [assistant] });
  assert.deepEqual(h.sent.map(message => message.content), ["Edited task", "Keep task"]);
});

test("a queued row can become steering at the next turn boundary", async t => {
  const h = await harness(t);
  await h.input("Steer this task");
  await h.input("Later task");
  h.press("previous");
  h.press("previous");
  h.press("toggle");
  h.press("enter");
  assert.equal(h.snapshot().rows[0].lane, "steer");
  assert.deepEqual(h.sent, []);
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent, [{ content: "Steer this task", options: { deliverAs: "steer" } }]);
  await h.emit("agent_end", { messages: [assistant] });
  assert.equal(h.sent[1].content, "Later task");
});

test("empty-composer Enter promotes the queue head immediately", async t => {
  const h = await harness(t);
  await h.input("Promoted task");
  await h.input("Later task");
  h.press("enter");
  assert.deepEqual(h.sent, [{ content: "Promoted task", options: { deliverAs: "steer" } }]);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Later task"]);
});

test("cancel rolls back text, position, lane and removal drafts", async t => {
  const h = await harness(t);
  await h.input("First task");
  await h.input("Second task");
  h.press("previous");
  h.editor.setText("Discarded edit");
  h.press("up");
  h.press("toggle");
  h.press("remove");
  h.press("cancel");
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent, []);
  await h.emit("agent_end", { messages: [assistant] });
  await h.emit("agent_end", { messages: [assistant] });
  assert.deepEqual(h.sent.map(message => message.content), ["First task", "Second task"]);
});

test("resumed queues stay paused until explicitly resumed", async t => {
  const entries = [{ type: "custom", customType: "pi-queue-steer:queue", data: {
    version: 1, paused: false,
    rows: [{ id: "follow-up-1", lane: "followUp", text: "Restored task", images: [], sequence: 1 }],
  } }];
  const h = await harness(t, "tui", entries);
  h.setIdle(true);
  await h.emit("agent_settled");
  assert.deepEqual(h.sent, []);
  h.press("enter");
  assert.deepEqual(h.sent, [{ content: "Restored task", options: undefined }]);
});

test("queue command registers help and rejects invalid arguments without mutation", async t => {
  const h = await harness(t);
  assert.ok(h.commands.has("queue"));
  assert.ok(h.commands.has("pause"));
  assert.ok(h.commands.has("queue-drain"));
  await h.command("");
  assert.match(h.notifications.at(-1).message, /Queue: 0 pending/);
  assert.match(h.notifications.at(-1).message, /Tab completes subcommands and row IDs/);
  for (const args of ["unknown", "add", "steer   ", "remove", "remove a b", "edit a b", "list extra", "clear extra", "resume extra"]) {
    await h.command(args);
    assert.equal(h.notifications.at(-1).level, "warning", args);
  }
  assert.equal(h.entries.length, 0);
  assert.deepEqual(h.sent, []);
  assert.equal(h.confirmations.length, 0);
});

test("idle command enqueue parks rows until explicit ordered resume", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add First task\n  Keep indentation");
  await h.command("add Second task");
  assert.deepEqual(h.sent, []);
  assert.equal(h.snapshot().paused, true);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["First task\n  Keep indentation", "Second task"]);
  await h.command("list");
  assert.match(h.notifications.at(-1).message, /follow-up-1/);
  assert.match(h.notifications.at(-1).message, /First task\n  Keep indentation/);
  await h.emit("agent_settled");
  assert.deepEqual(h.sent, []);
  await h.command("resume");
  assert.deepEqual(h.sent, [{ content: "First task\n  Keep indentation", options: undefined }]);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Second task"]);
  h.setIdle(false);
  await h.emit("agent_end", { messages: [assistant] });
  assert.equal(h.sent[1].content, "Second task");
});

test("busy command steering precedes future runs without merging follow-ups", async t => {
  const h = await harness(t);
  await h.command("add Future A");
  await h.command("steer Current A");
  await h.command("steer Current B");
  await h.command("add Future B");
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Current A", "Current B", "Future A", "Future B"]);
  assert.deepEqual(h.sent, []);
  await h.emit("turn_end", { message: assistant });
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent.map(message => message.options.deliverAs), ["steer", "steer"]);
  await h.emit("agent_end", { messages: [assistant] });
  await h.emit("agent_end", { messages: [assistant] });
  assert.deepEqual(h.sent.map(message => message.content), ["Current A", "Current B", "Future A", "Future B"]);
});

test("command enqueue preserves manual pause and graceful pause cannot be bypassed", async t => {
  const h = await harness(t);
  await h.command("add Original task");
  await h.emit("tool_execution_start", { toolCallId: "slow-tool", toolName: "bash" });
  await h.command("", "pause");
  await h.command("resume");
  await h.command("steer Blocked steer");
  assert.match(h.notifications.at(-1).message, /graceful pause/);
  assert.equal(h.aborts, 0);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Original task"]);
  await h.emit("tool_execution_end", { toolCallId: "slow-tool", toolName: "bash" });
  assert.equal(h.aborts, 1);
  h.setIdle(false);
  await h.command("add Later task");
  await h.command("steer Paused steer");
  assert.equal(h.snapshot().paused, true);
  await h.emit("turn_end", { message: assistant });
  await h.emit("agent_end", { messages: [assistant] });
  assert.deepEqual(h.sent, []);
  await h.command("resume");
  await h.emit("turn_end", { message: assistant });
  assert.equal(h.sent[0].content, "Paused steer");
});

test("queue edit selects by stable ID and reuses save, reorder, lane and cancel controls", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add First task");
  await h.command("add Latest task");
  h.editor.setText("Composer draft");
  await h.command("edit");
  assert.equal(h.editor.getText(), "Latest task");
  h.press("cancel");
  assert.equal(h.editor.getText(), "Composer draft");
  await h.command("edit follow-up-1");
  assert.equal(h.editor.getText(), "First task");
  h.editor.setText("Edited first task");
  h.press("down");
  h.press("toggle");
  for (const args of ["add Blocked", "remove follow-up-2", "clear", "resume", "edit follow-up-2"]) {
    await h.command(args);
    assert.match(h.notifications.at(-1).message, /Finish or cancel row editing/);
  }
  h.press("enter");
  assert.equal(h.editor.getText(), "Composer draft");
  assert.deepEqual(h.snapshot().rows.map(row => [row.text, row.lane]), [
    ["Latest task", "followUp"], ["Edited first task", "steer"],
  ]);
  assert.deepEqual(h.sent, []);
});

test("remove uses stable IDs, preserves drafts and writes the empty tombstone", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add First task");
  await h.command("add Second task");
  h.editor.setText("Unsent draft");
  const before = structuredClone(h.snapshot());
  await h.command("remove missing");
  await h.command("edit missing");
  assert.deepEqual(h.snapshot(), before);
  assert.match(h.notifications.at(-1).message, /use \/queue list or Tab/);
  await h.command("remove follow-up-1");
  assert.deepEqual(h.snapshot().rows.map(row => row.id), ["follow-up-2"]);
  assert.equal(h.editor.getText(), "Unsent draft");
  await h.command("remove follow-up-2");
  assert.deepEqual(h.snapshot().rows, []);
  assert.deepEqual(h.sent, []);
});

test("clear requires confirmation and never aborts the current run", async t => {
  const h = await harness(t);
  await h.command("add First task");
  await h.command("add Second task");
  h.editor.setText("Unsent draft");
  const before = structuredClone(h.snapshot());
  await h.command("clear");
  assert.equal(h.confirmations.length, 1);
  assert.deepEqual(h.snapshot(), before);
  h.setConfirmation(true);
  await h.command("clear");
  assert.deepEqual(h.snapshot().rows, []);
  assert.equal(h.aborts, 0);
  assert.equal(h.editor.getText(), "Unsent draft");
  assert.deepEqual(h.sent, []);
});

test("clear refuses a stale confirmation after the queue changes", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add Original task");
  h.setConfirmation(async () => {
    await h.command("add New task during confirmation");
    return true;
  });
  await h.command("clear");
  assert.match(h.notifications.at(-1).message, /Queue changed during confirmation/);
  assert.equal(h.snapshot().rows.length, 2);
});

test("clear confirmation cannot cross a session runtime boundary", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add Outgoing task");
  h.setConfirmation(async () => {
    await h.emit("session_shutdown", { reason: "new" });
    await h.emit("session_start", { reason: "new" });
    await h.command("add New session task");
    return true;
  });
  await h.command("clear");
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["New session task"]);
  assert.deepEqual(h.sent, []);
});

test("controls refuse mutation during an executing control or compaction", async t => {
  const h = await harness(t);
  await h.command("add Original task");
  const before = structuredClone(h.snapshot());
  await h.emit("session_before_compact", { reason: "auto" });
  for (const args of ["add Blocked", "steer Blocked", "edit", "remove follow-up-1", "clear", "resume"]) {
    await h.command(args);
    assert.match(h.notifications.at(-1).message, /current control command/);
  }
  await h.command("list");
  assert.match(h.notifications.at(-1).message, /control: auto-compact/);
  assert.deepEqual(h.snapshot(), before);
  assert.deepEqual(h.sent, []);
});

test("resume honors individual row holds and does not promote follow-ups to steer", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add Held task");
  await h.command("add Later task");
  await h.command("edit follow-up-1");
  h.press("hold");
  h.press("enter");
  assert.equal(h.snapshot().rows[0].paused, true, "Editing must persist the row hold");
  await h.command("resume");
  assert.deepEqual(h.sent, []);
  assert.equal(h.snapshot().rows[0].paused, true);
  assert.match(h.notifications.at(-1).message, /row follow-up-1 is held/);
  h.setIdle(false);
  await h.command("edit follow-up-1");
  h.press("hold");
  h.press("enter");
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent, []);
  await h.emit("agent_end", { messages: [assistant] });
  assert.equal(h.sent[0].options.deliverAs, "followUp");
  assert.equal(h.sent[0].content, "Held task");
});

test("resume command releases restored queues and command rows remain unexpanded", async t => {
  const entries = [{ type: "custom", customType: "pi-queue-steer:queue", data: {
    version: 1, paused: false,
    rows: [{ id: "follow-up-1", lane: "followUp", text: "Restored task", images: [], sequence: 1 }],
  } }];
  const h = await harness(t, "tui", entries);
  h.setIdle(true);
  await h.emit("agent_settled");
  assert.deepEqual(h.sent, []);
  await h.command("resume");
  assert.deepEqual(h.sent, [{ content: "Restored task", options: undefined }]);
  await h.command("add /reload");
  assert.equal(h.snapshot().rows[0].text, "/reload");
  assert.equal(h.sent.length, 1);
});

test("failed resume restores rows and does not overwrite the dispatch error", async t => {
  const h = await harness(t);
  h.setIdle(true);
  await h.command("add Retry task");
  h.failSend();
  await h.command("resume");
  assert.match(h.notifications.at(-1).message, /offline dispatch failure/);
  assert.equal(h.notifications.at(-1).level, "error");
  assert.equal(h.snapshot().paused, true);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Retry task"]);
  assert.deepEqual(h.sent, []);
});

test("completions carry usage hints and live row IDs through Pi's actual provider", async t => {
  const h = await harness(t);
  h.setIdle(true);
  const command = h.commands.get("queue");
  const provider = new tui.CombinedAutocompleteProvider([command], h.ctx.cwd, null);
  const suggestions = line => provider.getSuggestions([line], 0, line.length, { signal: new AbortController().signal });
  assert.ok((await suggestions("/que")).items.some(item => item.value === "queue"));
  const actions = await suggestions("/queue ");
  assert.deepEqual(actions.items.map(item => item.value.trim()), ["list", "add", "steer", "edit", "remove", "clear", "resume"]);
  assert.ok(actions.items.every(item => item.description));
  const add = await suggestions("/queue ad");
  const applied = provider.applyCompletion(["/queue ad"], 0, "/queue ad".length, add.items[0], add.prefix);
  assert.deepEqual(applied.lines, ["/queue add "]);
  assert.equal(await suggestions("/queue resume"), null, "Exact actions must not swallow Enter into completion");
  assert.equal(await suggestions("/queue add Some text"), null);
  assert.equal(await suggestions("/queue edit "), null);
  await h.command("add 中文任务\nSecond line");
  await h.command("steer Current task");
  const rows = await suggestions("/queue edit follow");
  assert.equal(rows.items[0].value, "edit follow-up-1");
  assert.match(rows.items[0].description, /中文任务 Second line/);
  const selected = provider.applyCompletion(["/queue edit follow"], 0, "/queue edit follow".length, rows.items[0], rows.prefix);
  assert.deepEqual(selected.lines, ["/queue edit follow-up-1"]);
  assert.equal(await suggestions("/queue edit follow-up-1"), null, "Exact row IDs must submit immediately");
  await h.command("remove follow-up-1");
  assert.equal(await suggestions("/queue remove follow"), null, "Removed IDs must disappear immediately");
  assert.equal((await suggestions("/queue remove steer")).items[0].value, "remove steer-2");
});

test("RPC publishes follow-up contents separately from steer and keeps native input unchanged", async t => {
  const h = await harness(t, "rpc");
  assert.equal(h.editor, undefined);
  assert.deepEqual(await h.input("Native input"), { action: "continue" });
  h.setIdle(true);
  await h.command("add First task\nKeep indentation");
  await h.command("add 中文任务");
  const text = h.statuses.get("queue");
  assert.match(text, /^Queue: 2 pending; paused/);
  assert.match(text, /follow-up-1: First task Keep indentation/);
  assert.match(text, /follow-up-2: 中文任务/);
  assert.deepEqual(h.queueMessages().items.map(row => [row.id, row.text]), [
    ["follow-up-1", "First task\nKeep indentation"], ["follow-up-2", "中文任务"],
  ]);
  const updates = h.statusUpdates.length;
  const widgetUpdates = h.widgetUpdates.length;
  await h.command("steer Current run");
  assert.equal(h.statuses.get("queue"), text, "Steers must not enter the follow-up status");
  assert.equal(h.statusUpdates.length, updates, "Unchanged previews must be deduplicated");
  assert.deepEqual(h.queueMessages().items.map(row => row.text), ["First task\nKeep indentation", "中文任务"], "Steers must not enter structured follow-ups");
  assert.ok(h.widgetUpdates.length >= widgetUpdates, "The dispatch revision may change when steers change");
  await h.command("list");
  assert.match(h.notifications.at(-1).message, /steer-3 \[steer\]/);
  const before = structuredClone(h.snapshot());
  await h.command("edit follow-up-1");
  assert.match(h.notifications.at(-1).message, /editing requires.*TUI/);
  assert.deepEqual(h.snapshot(), before);
  assert.deepEqual(h.sent, []);
  await h.command("remove follow-up-1");
  assert.match(h.statuses.get("queue"), /^Queue: 1 pending; paused.*follow-up-2: 中文任务/);
  h.setConfirmation(true);
  await h.command("clear");
  assert.equal(h.statuses.has("queue"), false);
  assert.equal(h.aborts, 0);
});

test("RPC delivery preserves lanes and removes consumed or shutdown status", async t => {
  const h = await harness(t, "rpc");
  await h.command("add Future A");
  await h.command("steer Current run");
  await h.command("add Future B");
  assert.match(h.statuses.get("queue"), /^Queue: 2 pending; active/);
  await h.emit("turn_end", { message: assistant });
  assert.deepEqual(h.sent, [{ content: "Current run", options: { deliverAs: "steer" } }]);
  await h.emit("agent_end", { messages: [assistant] });
  assert.match(h.statuses.get("queue"), /^Queue: 1 pending; active.*Future B/);
  await h.emit("agent_end", { messages: [assistant] });
  assert.equal(h.statuses.has("queue"), false);
  assert.deepEqual(h.sent.map(row => row.options.deliverAs), ["steer", "followUp", "followUp"]);
  h.setIdle(true);
  await h.command("add Parked task");
  await h.command("resume");
  assert.equal(h.sent.at(-1).content, "Parked task");
  assert.equal(h.statuses.has("queue"), false);
  await h.command("add Outgoing task");
  await h.emit("session_shutdown", { reason: "new" });
  assert.equal(h.queueMessages(), undefined);
  assert.equal(h.statuses.has("queue"), false);
  await h.emit("session_start", { reason: "new" });
  assert.equal(h.statuses.has("queue"), false);
});

test("RPC previews bound long text and rows while list retains complete content", async t => {
  const h = await harness(t, "rpc");
  h.setIdle(true);
  await h.command("add \u001b[32m检查\u001b[0m\n第二行\u0007");
  await h.command("add " + "文".repeat(20000));
  for (let i = 0; i < 12; i++) await h.command("add Task " + (i + 1));
  const text = h.statuses.get("queue");
  assert.match(text, /^Queue: 14 pending; paused/);
  assert.ok(text.includes("检查 第二行"));
  assert.ok(text.includes("文".repeat(400) + "…"));
  assert.ok(text.includes("+6 more"));
  assert.ok(text.length <= 4096);
  assert.equal(h.queueMessages().items.length, 14);
  assert.equal(h.queueMessages().items[0].text, "检查\n第二行");
  assert.equal(h.queueMessages().items[1].text.length, 4000);
  assert.equal(h.queueMessages().items[1].truncated, true);
  assert.doesNotMatch(text, /[\u0000-\u001f\u007f]/u);
  await h.command("list");
  assert.ok(h.notifications.at(-1).message.includes("文".repeat(20000)));
});

test("RPC restores held rows and keeps failed sends visible and paused", async t => {
  const entries = [{ type: "custom", customType: "pi-queue-steer:queue", data: {
    version: 1, paused: false,
    rows: [
      { id: "steer-1", lane: "steer", text: "Current run", images: [], sequence: 1 },
      { id: "follow-up-2", lane: "followUp", text: "Restored task", images: [], sequence: 2, paused: true },
    ],
  } }];
  const h = await harness(t, "rpc", entries);
  assert.match(h.statuses.get("queue"), /^Queue: 1 pending; paused.*follow-up-2 \(held\): Restored task/);
  await h.command("remove steer-1");
  await h.command("remove follow-up-2");
  assert.equal(h.statuses.has("queue"), false);
  h.setIdle(true);
  await h.command("add Retry task");
  h.failSend();
  await h.command("resume");
  assert.match(h.notifications.at(-1).message, /offline dispatch failure/);
  assert.match(h.statuses.get("queue"), /^Queue: 1 pending; paused.*Retry task/);
  assert.deepEqual(h.snapshot().rows.map(row => row.text), ["Retry task"]);
  assert.deepEqual(h.sent, []);
});

test("RPC snapshots bound large queues and expose image counts without image data", async t => {
  const image = { type: "image", mimeType: "image/png", data: "fixture-image-data" };
  const rows = Array.from({ length: 80 }, (_, i) => ({
    id: "follow-up-" + (i + 1), lane: "followUp", sequence: i + 1,
    text: i === 0 ? "" : "文\n".repeat(4000), images: i === 0 ? [image] : [], paused: i === 0,
  }));
  const h = await harness(t, "rpc", [{ type: "custom", customType: "pi-queue-steer:queue", data: { version: 1, rows, paused: true } }]);
  const messages = h.queueMessages();
  assert.equal(messages.total, 80);
  assert.equal(messages.items.length, 64);
  assert.equal(messages.items[0].paused, true);
  assert.equal(messages.items[0].imageCount, 1);
  assert.equal(messages.items[0].text, "");
  assert.ok(messages.items[1].truncated);
  const serialized = JSON.stringify(messages);
  assert.ok(serialized.length < 1_048_576);
  assert.ok(!serialized.includes(image.data));
});

const queueControl = async (h, action, extra = {}) => {
  const requestId = "test-control-" + action;
  await h.command(Buffer.from(JSON.stringify({
    action, requestId, revision: h.queueMessages()?.plan?.revision ?? "stale", ...extra,
  })).toString("base64url"), "queue-control");
  const result = h.controlResult();
  assert.equal(result.requestId, requestId);
  if (result.error) throw new Error(result.error);
};

test("RPC delivery plan follows the real head and preserves row holds", async t => {
  const h = await harness(t, "rpc");
  await h.command("steer Separate steering");
  await h.command("add Follow-up after steer");
  assert.equal(h.queueMessages().total, 1);
  assert.equal(h.queueMessages().plan.state, "steer-first");
  assert.equal(h.queueMessages().plan.headId, null);
  assert.equal(h.queueMessages().items[0].dispatchHead, false);
  await h.command("remove steer-1");
  assert.equal(h.queueMessages().plan.state, "after-run");
  assert.equal(h.queueMessages().items[0].dispatchHead, true);
  await queueControl(h, "row-pause", { id: "follow-up-2", paused: true });
  assert.equal(h.queueMessages().plan.state, "row-paused");
  await queueControl(h, "pause", { paused: true });
  assert.equal(h.queueMessages().plan.state, "paused");
  await queueControl(h, "pause", { paused: false });
  assert.equal(h.queueMessages().plan.state, "row-paused");
  assert.deepEqual(h.sent, []);
});

test("RPC controls edit full text on the same queue and reject stale or foreign edits", async t => {
  const h = await harness(t, "rpc");
  h.setIdle(true);
  const text = "文".repeat(65536);
  await h.command("add " + text);
  const revision = h.queueMessages().plan.revision;
  await queueControl(h, "begin-edit", { id: "follow-up-1", owner: "client-a" });
  assert.equal(h.queueMessages().plan.editor.text, text);
  assert.equal(h.queueMessages().items[0].held, true);
  await assert.rejects(queueControl(h, "save-edit", { id: "follow-up-1", owner: "other", text: "wrong" }), /edit has ended/);
  await assert.rejects(queueControl(h, "remove", { id: "follow-up-1", revision }), /plan changed/);
  await queueControl(h, "cancel-edit", { id: "follow-up-1", owner: "client-a" });
  assert.equal(h.snapshot().rows[0].text, text, "Cancel must preserve the full original");
  await queueControl(h, "begin-edit", { id: "follow-up-1", owner: "client-a" });
  await queueControl(h, "renew-edit", { id: "follow-up-1", owner: "client-a" });
  await queueControl(h, "save-edit", { id: "follow-up-1", owner: "client-a", text: "Edited\n正文" });
  assert.equal(h.snapshot().rows[0].text, "Edited\n正文");
  assert.equal(h.queueMessages().plan.editor, null);
  assert.equal(h.editor, undefined, "RPC must never use Pi's terminal editor");
  assert.deepEqual(h.sent, []);
});

test("RPC controls reorder and remove follow-ups without touching steer or attachments", async t => {
  const image = { type: "image", mimeType: "image/png", data: "preserved-image" };
  const rows = [
    { id: "follow-up-1", lane: "followUp", sequence: 1, text: "Image", images: [image], paused: false },
    { id: "steer-2", lane: "steer", sequence: 2, text: "Steer", images: [], paused: false },
    { id: "follow-up-3", lane: "followUp", sequence: 3, text: "Last", images: [], paused: false },
  ];
  const h = await harness(t, "rpc", [{ type: "custom", customType: "pi-queue-steer:queue", data: { version: 1, rows, paused: true } }]);
  await queueControl(h, "move", { id: "follow-up-3", direction: -1 });
  assert.deepEqual(h.queueMessages().items.map(row => row.id), ["follow-up-3", "follow-up-1"]);
  await assert.rejects(queueControl(h, "remove", { id: "steer-2" }), /follow-up message/);
  await queueControl(h, "begin-edit", { id: "follow-up-1", owner: "image-editor" });
  await queueControl(h, "save-edit", { id: "follow-up-1", owner: "image-editor", text: "New caption" });
  assert.deepEqual(h.snapshot().rows.find(row => row.id === "follow-up-1").images, [image]);
  await queueControl(h, "remove", { id: "follow-up-3" });
  await queueControl(h, "remove", { id: "follow-up-1" });
  assert.equal(h.queueMessages(), undefined);
  assert.deepEqual(h.snapshot().rows.map(row => row.id), ["steer-2"]);
});

for (const mode of ["json", "print"]) {
  test(mode + " input passes through without a TUI editor", async t => {
    const h = await harness(t, mode);
    assert.equal(h.editor, undefined);
    assert.deepEqual(await h.input("Headless prompt"), { action: "continue" });
    assert.deepEqual(h.sent, []);
    await h.command("add Must not enqueue");
    await h.command("clear");
    assert.match(h.notifications.at(-1).message, /requires Pi's interactive TUI/);
    assert.equal(h.statusUpdates.length, 0, "Non-UI modes must not emit queue statuses");
    assert.equal(h.widgetUpdates.length, 0, "Non-UI modes must not emit queue widgets");
    assert.equal(h.confirmations.length, 0);
    assert.equal(h.entries.length, 0);
    assert.deepEqual(h.sent, []);
  });
}
