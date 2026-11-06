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
  up: "\x1b[1;4A", down: "\x1b[1;4B", remove: "\x1bx", toggle: "\x1bt", cancel: "\x1b",
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
  let editor;
  let factory;
  let idle = false;
  let draft = "";
  const terminal = { requestRender() {}, terminal: { rows: 40, columns: 100 } };
  const ctx = {
    cwd, mode, hasUI: mode === "tui", isIdle: () => idle, isProjectTrusted: () => true,
    model: { contextWindow: 200000 },
    sessionManager: { getBranch: () => entries },
    abort() { idle = true; },
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
    },
  };
  loaded.runtime.sendUserMessage = (content, options) => sent.push({ content, options });
  loaded.runtime.appendEntry = (customType, data) => entries.push({ type: "custom", customType, data });
  loaded.runtime.getCommands = () => [];
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
    ctx, emit, input, press, sent, entries, notifications, snapshot,
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

for (const mode of ["rpc", "json", "print"]) {
  test(mode + " input passes through without a TUI editor", async t => {
    const h = await harness(t, mode);
    assert.equal(h.editor, undefined);
    assert.deepEqual(await h.input("Headless prompt"), { action: "continue" });
    assert.deepEqual(h.sent, []);
  });
}
