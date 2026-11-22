import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import http from "node:http";
import https from "node:https";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [modules, bridgeInput, notifyInput] = process.argv.slice(2).map(value => path.resolve(value));
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-cmux-"));
const oldEnv = { ...process.env };
const tty = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: root, PI_CODING_AGENT_DIR: path.join(root, "agent"), PI_OFFLINE: "1" });
const forbidden = () => { throw new Error("Network is forbidden in cmux tests"); };
globalThis.fetch = forbidden;
net.connect = net.createConnection = forbidden;
net.Socket.prototype.connect = forbidden;
http.request = http.get = https.request = https.get = forbidden;
syncBuiltinESMExports();
const host = await import(pathToFileURL(path.join(modules, "@earendil-works/pi-coding-agent/dist/index.js")));
after(() => {
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, oldEnv);
  if (tty) Object.defineProperty(process.stdout, "isTTY", tty);
  else delete process.stdout.isTTY;
  fs.rmSync(root, { recursive: true, force: true });
});
let sequence = 0;
async function harness(t, { mode = "tui", surface = true, disabled = false, missing = false, native = false } = {}) {
  const directory = path.join(root, String(++sequence));
  const agentDir = path.join(directory, "agent");
  const extensions = path.join(agentDir, "extensions");
  fs.mkdirSync(extensions, { recursive: true });
  process.env.PI_CODING_AGENT_DIR = agentDir;
  if (surface) process.env.CMUX_SURFACE_ID = "fixture-surface";
  else delete process.env.CMUX_SURFACE_ID;
  if (disabled) process.env.CMUX_PI_HOOKS_DISABLED = "1";
  else delete process.env.CMUX_PI_HOOKS_DISABLED;
  Object.defineProperty(process.stdout, "isTTY", { configurable: true, value: true });
  const source = path.join(directory, "native.mjs");
  if (!missing) fs.writeFileSync(source, [
    'export default function(pi) {',
    '  pi.on("session_start", (_event, ctx) => { ctx.calls.push("start"); ctx.ui[Symbol.for("cmux.pi.cmux-dialog-hooks")] = true; });',
    '  pi.on("agent_settled", (_event, ctx) => ctx.calls.push("completed"));',
    '  pi.on("session_shutdown", (_event, ctx) => { ctx.calls.push("stop"); delete ctx.ui[Symbol.for("cmux.pi.cmux-dialog-hooks")]; });',
    '}',
  ].join('\n'));
  fs.writeFileSync(path.join(extensions, "envy-cmux.ts"), fs.readFileSync(bridgeInput, "utf8").replace("@cmuxPiSource@", source));
  fs.copyFileSync(notifyInput, path.join(extensions, "envy-notify.ts"));
  if (native) fs.writeFileSync(path.join(extensions, "cmux-session.ts"), "// cmux-pi-session-extension-marker v4\nexport default function() {}\n");
  const loaded = await host.discoverAndLoadExtensions([], directory, agentDir);
  assert.deepEqual(loaded.errors, [], "The real Pi discovery must load the bridge");
  const ctx = { mode, hasUI: mode === "tui" || mode === "rpc", calls: [], warnings: [], ui: {},
    sessionManager: { getSessionId: () => "fixture-session" },
  };
  ctx.ui.notify = message => ctx.warnings.push(message);
  async function emit(name) {
    let output = "";
    const write = process.stdout.write;
    process.stdout.write = value => { output += value; return true; };
    try {
      for (const extension of loaded.extensions) {
        for (const handler of extension.handlers.get(name) || []) await handler({ type: name }, ctx);
      }
    } finally { process.stdout.write = write; }
    return output;
  }
  t.after(() => loaded.runtime.invalidate());
  return { ctx, emit, source, loaded };
}

test("cmux owns interactive completion while the generic notifier stays silent", async t => {
  const h = await harness(t);
  assert.equal(await h.emit("session_start"), "");
  assert.equal(await h.emit("agent_settled"), "");
  assert.deepEqual(h.ctx.calls, ["start", "completed"]);
  await h.emit("session_shutdown");
  assert.deepEqual(h.ctx.calls, ["start", "completed", "stop"]);
});

test("iTerm2 keeps its existing OSC notification", async t => {
  const h = await harness(t, { surface: false });
  await h.emit("session_start");
  assert.equal(await h.emit("agent_settled"), String.fromCharCode(27) + "]777;notify;Pi;Ready for input" + String.fromCharCode(7));
  assert.deepEqual(h.ctx.calls, []);
});

test("print, JSON, RPC and SDK contexts never send agent notifications", async t => {
  for (const mode of ["print", "json", "rpc", "sdk"]) {
    const h = await harness(t, { mode });
    await h.emit("session_start");
    assert.equal(await h.emit("agent_settled"), "", mode);
    await h.emit("session_shutdown");
    assert.deepEqual(h.ctx.calls, [], mode);
  }
});

test("a headless child inheriting a cmux environment cannot notify", async t => {
  const h = await harness(t);
  h.ctx.hasUI = false;
  await h.emit("session_start");
  assert.equal(await h.emit("agent_settled"), "");
  assert.deepEqual(h.ctx.calls, []);
});

test("disabled or missing native bridges retain generic notifications", async t => {
  for (const options of [{ disabled: true }, { missing: true }]) {
    const h = await harness(t, options);
    await h.emit("session_start");
    assert.ok((await h.emit("agent_settled")).includes("Ready for input"));
    assert.deepEqual(h.ctx.calls, []);
    assert.equal(h.ctx.warnings.length, options.missing ? 1 : 0);
  }
});

test("an existing cmux-managed extension is not registered a second time", async t => {
  const h = await harness(t, { native: true });
  await h.emit("session_start");
  await h.emit("agent_settled");
  assert.deepEqual(h.ctx.calls, []);
});

test("reloading through real Pi discovery does not retain old registrations", async t => {
  for (let index = 0; index < 2; index++) {
    const h = await harness(t);
    await h.emit("session_start");
    await h.emit("agent_settled");
    assert.deepEqual(h.ctx.calls, ["start", "completed"]);
  }
});
