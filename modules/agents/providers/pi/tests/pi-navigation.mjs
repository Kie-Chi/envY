// Exercise real overlay, widget, and host input dispatch without a model or terminal.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import tls from "node:tls";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";

const [nodeModules, btwPackage, goalPackage, bindingsJson] = process.argv.slice(2);
assert.ok(nodeModules && btwPackage && goalPackage && bindingsJson,
  "Usage: node pi-navigation.mjs <Pi node_modules> <btw> <goal> <JSON keybindings>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-navigation-"));
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: root, PI_OFFLINE: "1", PI_CODING_AGENT_DIR: path.join(root, "agent") });
const denyNetwork = () => { throw new Error("Network is forbidden in navigation tests"); };
globalThis.fetch = denyNetwork;
net.Socket.prototype.connect = denyNetwork;
tls.connect = denyNetwork;
syncBuiltinESMExports();
after(() => fs.rmSync(root, { recursive: true, force: true }));
const require = createRequire(path.join(nodeModules, "navigation-test.cjs"));
const { createJiti } = require("jiti");
const aliases = Object.fromEntries(["pi-ai", "pi-coding-agent", "pi-tui", "pi-agent-core"].map(name => [
  "@earendil-works/" + name, path.join(nodeModules, "@earendil-works/" + name + "/dist/index.js"),
]));
const jiti = createJiti(fileURLToPath(import.meta.url), { alias: aliases, moduleCache: true, fsCache: false });
const { BtwOverlayController } = await jiti.import(path.join(btwPackage, "btw-ui.ts"));
const { TuiAltScreen } = await jiti.import(path.join(nodeModules, "@earendil-works/pi-tui/dist/tui-alt-screen.js"));
const { getKeybindings } = await jiti.import(path.join(nodeModules, "@earendil-works/pi-tui/dist/keybindings.js"));
const { syncTerminalInputPause } = await jiti.import(path.join(goalPackage, "extensions/goal-widget.ts"));
const { GoalWidgetComponent } = await jiti.import(path.join(goalPackage, "extensions/widgets/goal-widget.ts"));
const { createGoal } = await jiti.import(path.join(goalPackage, "extensions/goal-record.ts"));
const theme = { fg: (_color, value) => value, bg: (_color, value) => value,
  bold: value => value, strikethrough: value => value };
const keys = { up: "\x1b[A", down: "\x1b[B", pageUp: "\x1b[5~", pageDown: "\x1b[6~", home: "\x1b[H", end: "\x1b[F" };
function overlay() {
  const terminal = { columns: 80, rows: 24, write() {}, hideCursor() {}, showCursor() {} };
  const tui = new TuiAltScreen(terminal);
  tui.requestRender = () => {};
  const controller = new AbortController();
  let closed = false;
  const component = new BtwOverlayController("Offline navigation", [], theme, tui,
    () => { closed = true; }, controller, () => {});
  component.setAnswer(Array.from({ length: 80 }, (_, index) => "ANSWER_" + String(index).padStart(3, "0")).join("\n"));
  const render = () => component.render(terminal.columns);
  render();
  return { component, tui, terminal, controller, render, closed: () => closed };
}
const firstAnswer = lines => Number(lines.find(line => /ANSWER_\d+/.test(line))?.match(/ANSWER_(\d+)/)[1]);

test("BTW arrows reveal older content upward and clamp at both ends", () => {
  const fixture = overlay();
  const bottom = fixture.render();
  fixture.component.handleInput(keys.up);
  assert.equal(firstAnswer(fixture.render()), firstAnswer(bottom) - 1);
  fixture.component.handleInput(keys.down);
  assert.deepEqual(fixture.render(), bottom);
  for (let index = 0; index < 100; index++) fixture.component.handleInput(keys.up);
  const top = fixture.render();
  fixture.component.handleInput(keys.up);
  assert.deepEqual(fixture.render(), top);
  fixture.component.handleInput(keys.down);
  assert.notDeepEqual(fixture.render(), top, "one Down must move immediately after repeated Up at the boundary");
  for (let index = 0; index < 100; index++) fixture.component.handleInput(keys.down);
  assert.deepEqual(fixture.render(), bottom);
});

test("BTW paging, edges, and resizing preserve a usable viewport", () => {
  const fixture = overlay();
  const bottom = fixture.render();
  fixture.component.handleInput(keys.pageUp);
  assert.ok(firstAnswer(fixture.render()) < firstAnswer(bottom) - 1);
  fixture.component.handleInput(keys.pageDown);
  assert.deepEqual(fixture.render(), bottom);
  fixture.component.handleInput(keys.home);
  assert.match(fixture.render().join("\n"), /ANSWER_000/);
  fixture.terminal.rows = 12;
  fixture.terminal.columns = 24;
  assert.ok(fixture.render().length <= Math.floor(12 * 0.85));
  fixture.component.handleInput(keys.end);
  assert.match(fixture.render().join("\n"), /ANSWER_079/);
});

test("BTW consumes normalized wheel events including at the scroll boundary", () => {
  const fixture = overlay();
  const bottom = fixture.render();
  const event = { type: "wheel", button: "none", x: 1, y: 1, screenX: 1, screenY: 1,
    width: 80, height: 20, shift: false, alt: false, ctrl: false, wheelDelta: -3 };
  assert.equal(fixture.component.handleMouse(event)?.handled, true);
  assert.equal(firstAnswer(fixture.render()), firstAnswer(bottom) - 3);
  assert.equal(fixture.component.handleMouse({ ...event, wheelDelta: 1000 })?.handled, true);
  assert.deepEqual(fixture.render(), bottom);
  assert.equal(fixture.component.handleMouse({ ...event, wheelDelta: 3 })?.handled, true);
  assert.deepEqual(fixture.render(), bottom);
});

test("focused BTW owns keyboard input and restores the editor when dismissed", () => {
  const fixture = overlay();
  let editorKeys = 0;
  let hostMoves = 0;
  const editor = { render: () => ["EDITOR"], invalidate() {}, handleInput() { editorKeys++; } };
  fixture.tui.setFocus(editor);
  const handle = fixture.tui.showOverlay(fixture.component, { width: "100%", anchor: "bottom-center" });
  fixture.tui.scrollToPrompt = () => { hostMoves++; };
  const before = fixture.render();
  fixture.tui.handleTerminalInput(keys.up);
  assert.ok(firstAnswer(fixture.render()) < firstAnswer(before));
  fixture.tui.handleTerminalInput("\x1b[1;6A");
  assert.equal(hostMoves, 0);
  assert.equal(editorKeys, 0);
  fixture.tui.handleTerminalInput("\x1b");
  assert.equal(fixture.closed(), true);
  assert.equal(fixture.controller.signal.aborted, true);
  handle.hide();
  fixture.tui.handleTerminalInput("a");
  assert.equal(editorKeys, 1);
});

test("generated keybindings route compact Goal scrolling ahead of chat navigation", () => {
  getKeybindings().setUserBindings(JSON.parse(bindingsJson));
  const tui = new TuiAltScreen({ columns: 120, rows: 40, write() {} });
  tui.requestRender = () => {};
  let hostMoves = 0;
  tui.scrollToPrompt = () => { hostMoves++; };
  const goal = { ...createGoal({ objective: "Offline task navigation", autoContinue: false }),
    taskList: { tasks: Array.from({ length: 20 }, (_, index) => ({ id: "task-" + index,
      title: "TASK_" + String(index).padStart(3, "0"), status: "pending" })) } };
  const widget = new GoalWidgetComponent({ tui, theme, getGoal: () => goal, getSettings: () => ({}) });
  const core = { goalModalDepth: 0, state: { goal }, isDashboardExpanded: () => false,
    goalWidgetComponentRef: { current: widget } };
  const ctx = { hasUI: true, cwd: root, isIdle: () => true,
    ui: { onTerminalInput: handler => tui.addInputListener(handler) } };
  const render = () => widget.render(120);
  const before = render();
  syncTerminalInputPause(core, ctx);
  tui.handleTerminalInput("\x1b[1;6B");
  const down = render();
  assert.notDeepEqual(down, before, "Goal must visibly scroll down");
  assert.equal(hostMoves, 0);
  tui.handleTerminalInput("\x1b[1;6A");
  assert.deepEqual(render(), before);
  tui.handleTerminalInput("\x1b[1;5A");
  tui.handleTerminalInput("\x1b[1;5B");
  assert.equal(hostMoves, 2, "Ctrl+arrows must retain chat navigation");
  core.terminalInputUnsubscribe();
});
