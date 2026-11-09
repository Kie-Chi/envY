import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [contextPackage, nodeModules] = process.argv.slice(2);
assert.ok(contextPackage && nodeModules,
  "Usage: node pi-context.mjs <contextPackage> <Pi node_modules>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-context-"));
const home = path.join(root, "home");
const tmp = path.join(root, "tmp");
for (const directory of [home, tmp, path.join(home, ".config"), path.join(home, ".cache"), path.join(home, ".local/share"), path.join(home, ".local/state")]) {
  fs.mkdirSync(directory, { recursive: true });
}
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
const cwd = path.join(root, "project");
fs.mkdirSync(path.join(agentDir, "extensions"), { recursive: true });
fs.mkdirSync(cwd);
fs.symlinkSync(fs.realpathSync(contextPackage), path.join(agentDir, "extensions/envy-context"));
fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({
  retry: { enabled: false }, compaction: { enabled: false },
}));
globalThis.fetch = () => { throw new Error("Network access is forbidden in context tests"); };
const hostPath = path.join(nodeModules, "@earendil-works/pi-coding-agent/dist");
const host = await import(pathToFileURL(path.join(hostPath, "index.js")));
const ai = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-ai/dist/index.js")));
const themes = await import(pathToFileURL(path.join(hostPath, "modes/interactive/theme/theme.js")));
host.initTheme("dark", false);

let requests = 0;
const stream = (model) => {
  requests++;
  const output = ai.createAssistantMessageEventStream();
  const message = {
    role: "assistant", api: model.api, provider: model.provider, model: model.id,
    timestamp: Date.now(), stopReason: "stop",
    usage: { input: 12, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 15,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    content: [{ type: "text", text: "Offline context reply" }],
  };
  output.push({ type: "start", partial: message });
  output.push({ type: "text_start", contentIndex: 0, partial: message });
  output.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
  output.push({ type: "text_end", contentIndex: 0, partial: message });
  output.push({ type: "done", reason: message.stopReason, message });
  output.end();
  return output;
};
fs.writeFileSync(path.join(agentDir, "extensions/offline-fixture.ts"), `
export default function(pi) {
  pi.registerProvider("envy-offline", {
    baseUrl: "http://invalid.test", apiKey: "offline-test-only", api: "envy-offline-api",
    streamSimple: (model) => globalThis.__envyContextStream(model),
    models: [{
      id: "inherited", name: "inherited", reasoning: false, input: ["text"],
      contextWindow: 200000, maxTokens: 4096,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }],
  });
}
`);
globalThis.__envyContextStream = stream;
const originalCwd = process.cwd();
process.chdir(cwd);
let session;
after(async () => {
  try {
    if (session) {
      await session.extensionRunner.emit({ type: "session_shutdown", reason: "exit" });
      session.dispose();
    }
  } finally {
    process.chdir(originalCwd);
    delete globalThis.__envyContextStream;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

const loader = new host.DefaultResourceLoader({ cwd, agentDir });
await loader.reload();
assert.deepEqual(loader.getExtensions().errors, []);
const contextExtension = loader.getExtensions().extensions.find(extension => extension.path.includes("envy-context"));
assert.ok(contextExtension, "the real context extension must be discovered");
const command = contextExtension.commands.get("context");
assert.ok(command, "/context must be registered by the real extension");
const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
({ session } = await host.createAgentSession({
  cwd, agentDir, resourceLoader: loader, modelRuntime: runtime,
  sessionManager: host.SessionManager.inMemory(cwd),
}));
await session.bindExtensions({});
const model = new host.ModelRegistry(session.modelRuntime).find("envy-offline", "inherited");
assert.ok(model, "offline provider model must be registered");
await session.setModel(model);
await session.bindExtensions({});

async function runContext(args) {
  const rendered = [];
  const notices = [];
  const base = session.extensionRunner.createCommandContext();
  const ctx = Object.create(base);
  Object.defineProperties(ctx, {
    mode: { value: "tui" },
    hasUI: { value: true },
    ui: { value: {
      custom: async (factory) => {
        const tui = { terminal: { rows: 50, columns: 120 }, requestRender() {} };
        const component = factory(tui, themes.theme, {}, () => {});
        rendered.push(component.render(120).join("\n"));
      },
      notify: (message, level) => notices.push(`${level}: ${message}`),
      setStatus() {},
      setWorkingVisible() {},
    } },
  });
  await command.handler(args, ctx);
  return { text: rendered.join("\n"), notices };
}

test("before any request /context usage is estimated and makes no model request", { timeout: 60000 }, async () => {
  const result = await runContext("usage");
  assert.equal(requests, 0, "opening a view must not contact a provider");
  assert.match(result.text, /Context Usage/);
  assert.match(result.text, /Automatic model probing is disabled by envY/);
  assert.match(result.text, /Estimated context for the next model request/);
});

test("a natural offline request feeds both context views without extra model traffic", { timeout: 60000 }, async () => {
  await session.prompt("hello offline");
  assert.equal(requests, 1, "the natural turn must use exactly one offline request");
  await new Promise(resolve => setTimeout(resolve, 200));
  const usage = await runContext("usage");
  assert.equal(requests, 1, "opening usage after a natural request must not probe");
  assert.match(usage.text, /Context Usage/);
  assert.match(usage.text, /System Prompt/);
  assert.match(usage.text, /User Messages/);
  assert.doesNotMatch(usage.text, /Automatic model probing is disabled by envY/);
  const injections = await runContext("injections");
  assert.equal(requests, 1, "opening injections must not probe");
  assert.match(injections.text, /System Prompt/);
  assert.match(injections.text, /TOTAL \(Prompt, Tools, Injections\)/);
  assert.match(injections.text, /Latest Request/);
  assert.match(injections.text, /Built-in Tools/);
});
