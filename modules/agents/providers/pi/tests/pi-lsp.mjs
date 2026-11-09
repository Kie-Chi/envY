import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [extensionPath, nodeModules, serverCommandJson] = process.argv.slice(2);
assert.ok(extensionPath && nodeModules && serverCommandJson,
  "Usage: node pi-lsp.mjs <lsp package> <Pi node_modules> <JSON server command>");
const serverCommand = JSON.parse(serverCommandJson);
assert.ok(Array.isArray(serverCommand) && serverCommand.length > 1 && serverCommand[0].startsWith("/"),
  "The test requires an absolute Ruff command with an explicit argument");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-lsp-test-"));
const home = path.join(root, "home");
const tmp = path.join(root, "tmp");
for (const directory of [home, tmp, path.join(home, ".config"), path.join(home, ".cache"), path.join(home, ".local/share"), path.join(home, ".local/state")]) {
  fs.mkdirSync(directory, { recursive: true });
}
// Isolate the Pi host and Ruff from the invoking user's settings, credentials,
// caches, project configuration, and network.
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, {
  PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
  HOME: home,
  TMPDIR: tmp,
  XDG_CONFIG_HOME: path.join(home, ".config"),
  XDG_CACHE_HOME: path.join(home, ".cache"),
  XDG_DATA_HOME: path.join(home, ".local/share"),
  XDG_STATE_HOME: path.join(home, ".local/state"),
  PI_CODING_AGENT_DIR: path.join(root, "agent"),
});
const agentDir = process.env.PI_CODING_AGENT_DIR;
fs.mkdirSync(path.join(agentDir, "extensions"), { recursive: true });
fs.symlinkSync(fs.realpathSync(extensionPath), path.join(agentDir, "extensions/envy-lsp"));

const require = createRequire(import.meta.url);
const networkBlocked = () => { throw new Error("Network access is forbidden in LSP tests"); };
globalThis.fetch = networkBlocked;
for (const name of [ "http", "https" ]) {
  const module = require(`node:${name}`);
  module.request = networkBlocked;
  module.get = networkBlocked;
}
for (const name of [ "net", "tls" ]) {
  const module = require(`node:${name}`);
  module.connect = networkBlocked;
  module.createConnection = networkBlocked;
}
const childProcess = require("node:child_process");
const originalSpawn = childProcess.spawn;
const spawns = [];
childProcess.spawn = function(command, args, options) {
  const child = originalSpawn.call(this, command, args, options);
  const record = { command, args, child, pid: child.pid, didOpen: false, exited: false, exitCode: null };
  spawns.push(record);
  child.once("exit", (code, signal) => { record.exited = true; record.exitCode = code; record.signal = signal; });
  if (child.stdin) {
    const write = child.stdin.write.bind(child.stdin);
    child.stdin.write = (chunk, ...rest) => {
      const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
      if (text.includes("textDocument/didOpen")) record.didOpen = true;
      return write(chunk, ...rest);
    };
  }
  return child;
};
syncBuiltinESMExports();

const hostPath = path.join(nodeModules, "@earendil-works/pi-coding-agent/dist");
const host = await import(pathToFileURL(path.join(hostPath, "index.js")));
const loader = new host.DefaultResourceLoader({ cwd: root, agentDir });
await loader.reload();
assert.deepEqual(loader.getExtensions().errors, []);
const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
const { session } = await host.createAgentSession({
  cwd: root, agentDir, resourceLoader: loader, modelRuntime: runtime,
  sessionManager: host.SessionManager.inMemory(root),
});
await session.bindExtensions({});
const findTool = name => {
  const found = session.agent.state.tools.find(candidate => candidate.name === name);
  assert.ok(found, `${name} must be registered by the real extension`);
  return found;
};
const ruffSpawns = () => spawns.filter(record => record.command === serverCommand[0]);
const waitFor = async (predicate, message, timeout = 5000) => {
  const deadline = Date.now() + timeout;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 0));
  assert.ok(predicate(), message);
};
const waitForExit = async (record, timeout = 5000) => {
  await waitFor(() => record.exited, "Ruff must exit after the tool call", timeout);
};
after(async () => {
  try { session.dispose(); } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("real Pi session runs Ruff diagnostics and source.fixAll preview", { timeout: 60000 }, async () => {
  const source = path.join(root, "sample.py");
  const before = 'import os\n\nprint("ready")\n';
  fs.writeFileSync(source, before);
  const started = ruffSpawns().length;
  const diagnostics = await findTool("lsp_diagnostics").execute("diag", { paths: [ "sample.py" ] });
  const text = diagnostics.content[0].text;
  assert.match(text, /envy-ruff LSP diagnostics: [1-9][0-9]* diagnostic/);
  assert.match(text, /F401/);
  assert.equal(diagnostics.details.routes[0].server, "envy-ruff");
  assert.equal(diagnostics.details.routes[0].details.files[0].diagnostics[0].code, "F401");
  const preview = await findTool("lsp_fix").execute("fix", {
    path: "sample.py", server: "envy-ruff", kind: "source.fixAll",
  });
  assert.equal(preview.details.changed, true);
  assert.equal(preview.details.write, false);
  assert.ok(preview.details.edits.length > 0, "source.fixAll must produce real edits");
  assert.ok(!preview.details.text.includes("import os"));
  assert.equal(fs.readFileSync(source, "utf8"), before, "preview must not write the source file");
  const records = ruffSpawns().slice(started);
  assert.equal(records.length, 2, "diagnostics and preview must each start one real Ruff process");
  for (const record of records) await waitForExit(record);
  assert.ok(records.every(record => record.exited), "both completed Ruff processes must be gone");
});

test("cancellation stops a real Ruff process after initialize", { timeout: 60000 }, async () => {
  const source = path.join(root, "large.py");
  fs.writeFileSync(source, Array.from({ length: 12000 }, (_, i) => `value_${i} = ${i}`).join("\n") + "\n");
  const started = ruffSpawns().length;
  const controller = new AbortController();
  let settled = false;
  const promise = findTool("lsp_diagnostics").execute("cancel", { paths: [ "large.py" ] }, controller.signal);
  promise.then(() => { settled = true; }, () => { settled = true; });
  await waitFor(() => ruffSpawns().slice(started).some(record => record.didOpen),
    "Ruff must initialize and open the document before cancellation");
  const record = ruffSpawns().slice(started).find(candidate => candidate.didOpen);
  assert.ok(record?.pid, "the real Ruff process must have a pid");
  process.kill(record.pid, 0);
  assert.equal(settled, false, "the tool must still be running when cancellation arrives");
  controller.abort(new Error("test cancellation"));
  await assert.rejects(promise, /aborted|closed|exited|LSP/i);
  await waitForExit(record);
  assert.equal(record.exited, true, "cancellation must terminate the Ruff process");
});

test("a bad Ruff startup is reported as a startup failure, not a cancellation", { timeout: 60000 }, async () => {
  const userConfig = path.join(agentDir, "pi-lsp.json");
  fs.writeFileSync(userConfig, JSON.stringify({
    servers: { "envy-ruff": { command: [ ...serverCommand, "server" ], extensions: [ ".py", ".pyi" ] } },
  }));
  fs.writeFileSync(path.join(root, "bad.py"), "value = 1\n");
  const started = ruffSpawns().length;
  try {
    await assert.rejects(
      findTool("lsp_diagnostics").execute("bad-start", { paths: [ "bad.py" ] }),
      /failed to start|exited before response|server exited|LSP server exited/i,
    );
    const records = ruffSpawns().slice(started);
    assert.ok(records.length > 0, "the bad server command must have started");
    for (const record of records) await waitForExit(record);
    assert.ok(records.some(record => record.exitCode !== 0), "the bad startup must have a non-zero exit");
  } finally {
    fs.rmSync(userConfig, { force: true });
  }
});
