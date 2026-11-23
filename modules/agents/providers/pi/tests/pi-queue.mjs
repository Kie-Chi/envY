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
  let confirmation = false;
  let aborts = 0;
  let editor;
  let factory;
  let idle = false;
  let draft = "";
  const terminal = { requestRender() {}, terminal: { rows: 40, columns: 100 } };
  const ctx = {
    cwd, mode, hasUI: mode === "tui", isIdle: () => idle, isProjectTrusted: () => true,
    model: { contextWindow: 200000 },
    sessionManager: { getBranch: () => entries },
    abort() { aborts += 1; idle = true; },
    ui: {
      theme: themes.theme,
      getEditorComponent: () => factory,
      setEditorComponent(value) {
        factory = value;
        editor = value?.(terminal, themes.getEditorTheme(), keybindings);
      },
      getEditorText: () => editor?.getText() ?? draft,
      setEditorText(value) { draft = value; editor?.setText(value); },
      setWidget() {},
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
    ctx, emit, input, press, sent, entries, notifications, confirmations, snapshot,
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

for (const mode of ["rpc", "json", "print"]) {
  test(mode + " input passes through without a TUI editor", async t => {
    const h = await harness(t, mode);
    assert.equal(h.editor, undefined);
    assert.deepEqual(await h.input("Headless prompt"), { action: "continue" });
    assert.deepEqual(h.sent, []);
    await h.command("add Must not enqueue");
    await h.command("clear");
    assert.match(h.notifications.at(-1).message, /requires Pi's interactive TUI/);
    assert.equal(h.confirmations.length, 0);
    assert.equal(h.entries.length, 0);
    assert.deepEqual(h.sent, []);
  });
}
