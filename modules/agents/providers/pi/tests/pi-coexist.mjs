import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [nodeModules, sourcesJson, ruffCommand, roleDirectory] = process.argv.slice(2);
assert.ok(nodeModules && sourcesJson, "Usage: node pi-coexist.mjs <Pi node_modules> <JSON extension source paths> [absolute Ruff command] [role directory]");
const sources = JSON.parse(sourcesJson);
assert.ok(Array.isArray(sources) && sources.length > 0);
assert.ok(sources.every(source => path.isAbsolute(source) && !/cron/i.test(source)), "Only absolute non-cron sources are accepted");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-coexist-"));
const agentDir = path.join(root, "agent");
const cwd = path.join(root, "project");
const home = path.join(root, "home");
for (const directory of [cwd, home, path.join(root, "tmp"), path.join(agentDir, "extensions"), path.join(agentDir, "agents")]) fs.mkdirSync(directory, { recursive: true });
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: home, TMPDIR: path.join(root, "tmp"), PATH: "/usr/bin:/bin:/usr/sbin:/sbin", PI_CODING_AGENT_DIR: agentDir });
sources.forEach((source, index) => fs.symlinkSync(fs.realpathSync(source), path.join(agentDir, "extensions", `envy-${index}${source.endsWith(".ts") ? ".ts" : ""}`)));
if (roleDirectory) {
  assert.ok(path.isAbsolute(roleDirectory), "role directory must be absolute");
  for (const name of fs.readdirSync(roleDirectory)) if (name.endsWith(".md")) fs.copyFileSync(path.join(roleDirectory, name), path.join(agentDir, "agents", name));
}
fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({
  defaultTools: ["+codemode"], codemode: { mode: "only" },
  retry: { enabled: false }, compaction: { enabled: false },
}));
if (ruffCommand) {
  assert.ok(path.isAbsolute(ruffCommand));
  fs.writeFileSync(path.join(agentDir, "pi-lsp.json"), JSON.stringify({
    servers: { "envy-ruff": { command: [ruffCommand, "server"], extensions: [".py", ".pyi"] } },
  }));
}
globalThis.fetch = () => { throw new Error("Network access is forbidden in coexist tests"); };
fs.writeFileSync(path.join(agentDir, "extensions/offline.ts"), `
export default function(pi) {
  pi.registerProvider("envy-offline", {
    baseUrl: "http://invalid.test", apiKey: "offline-only", api: "envy-offline-api",
    streamSimple: (model, context) => globalThis.__envyCoexistStream(model, context),
    models: [{ id: "offline", name: "offline", reasoning: false, input: ["text"], contextWindow: 200000,
      maxTokens: 4096, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
  });
}
`);
const originalCwd = process.cwd();
process.chdir(cwd);
const host = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/index.js")));
host.initTheme("dark", false);
const ai = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-ai/dist/index.js")));
let exposed;
let diagnosticResult;
globalThis.__envyCoexistStream = (model, context) => {
  exposed = ai.getCurrentTools(context.messages);
  const result = context.messages.findLast(message => message.role === "toolResult" && message.toolName === "codemode");
  if (result) diagnosticResult = result;
  const message = {
    role: "assistant", api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
    stopReason: result ? "stop" : "toolUse",
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    content: result ? [{ type: "text", text: "Offline diagnostics complete" }] : [{ type: "toolCall", id: "coexist-diagnostics", name: "codemode",
      arguments: { code: 'return await tools.lsp_diagnostics({paths: ["sample.py"]});' } }],
  };
  const stream = ai.createAssistantMessageEventStream();
  stream.push({ type: "start", partial: message });
  stream.push({ type: "done", reason: message.stopReason, message });
  stream.end();
  return stream;
};
const loader = new host.DefaultResourceLoader({ cwd, agentDir,
  extensionFactories: [{ name: "codemode", builtin: true, replaceable: true, factory: host.createCodemodeExtension() }],
});
await loader.reload();
assert.deepEqual(loader.getExtensions().errors, []);
const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
const { session } = await host.createAgentSession({ cwd, agentDir, resourceLoader: loader, modelRuntime: runtime,
  sessionManager: host.SessionManager.inMemory(cwd) });
after(async () => {
  try { await session.extensionRunner.emit({ type: "session_shutdown", reason: "exit" }); session.dispose(); }
  finally { delete globalThis.__envyCoexistStream; process.chdir(originalCwd); fs.rmSync(root, { recursive: true, force: true }); }
});
await session.bindExtensions({});

test("all supplied real extensions register together with builtin codemode only", () => {
  const extensions = loader.getExtensions().extensions;
  const commands = extensions.flatMap(extension => [...extension.commands.keys()]);
  assert.equal(new Set(commands).size, commands.length, "commands must not collide");
  for (const command of ["context", "diff"]) assert.ok(commands.includes(command), command);
  const registered = extensions.flatMap(extension => [...extension.tools.keys()]);
  assert.equal(new Set(registered).size, registered.length, "extension tools must not collide");
  for (const name of ["subagent", "lsp_diagnostics"]) assert.ok(registered.includes(name), name);
  assert.ok(session.agent.state.tools.some(tool => tool.name === "codemode"));

});

test("real codemode calls real LSP diagnostics and returns Ruff F401", { timeout: 60000 }, async () => {
  const source = 'import os\n\nprint("ready")\n';
  fs.writeFileSync(path.join(cwd, "sample.py"), source);
  const codemode = session.agent.state.tools.find(tool => tool.name === "codemode");
  assert.ok(codemode);
  const model = new host.ModelRegistry(runtime).find("envy-offline", "offline");
  assert.ok(model);
  await session.setModel(model);
  await session.bindExtensions({});
  await session.prompt("Run offline Ruff diagnostics through codemode.");
  assert.ok(diagnosticResult, "a real assistant tool call must execute codemode");
  assert.notEqual(diagnosticResult.isError, true, JSON.stringify(diagnosticResult));
  assert.match(JSON.stringify(diagnosticResult), /F401/);
  assert.ok(exposed.some(tool => tool.name === "codemode"));
  assert.ok(!exposed.some(tool => tool.name === "lsp_diagnostics"), "only mode must hide LSP from model exposure");
  assert.equal(fs.readFileSync(path.join(cwd, "sample.py"), "utf8"), source);
});
