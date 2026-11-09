import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [subagentsPackage, nodeModules, roleDirectory] = process.argv.slice(2);
assert.ok(subagentsPackage && nodeModules && roleDirectory,
  "Usage: node pi-subagents.mjs <subagentsPackage> <Pi node_modules> <roleDirectory>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-subagents-"));
const agentDir = path.join(root, "agent");
const cwd = path.join(root, "project");
process.env.PI_CODING_AGENT_DIR = agentDir;
fs.mkdirSync(path.join(agentDir, "extensions"), { recursive: true });
fs.mkdirSync(path.join(agentDir, "agents"));
fs.mkdirSync(cwd);
fs.symlinkSync(fs.realpathSync(subagentsPackage), path.join(agentDir, "extensions/envy-subagents"));
for (const name of fs.readdirSync(roleDirectory)) {
  if (name.endsWith(".md")) fs.copyFileSync(path.join(roleDirectory, name), path.join(agentDir, "agents", name));
}
fs.writeFileSync(path.join(cwd, "evidence.txt"), "offline child evidence\n");
const startupLog = path.join(root, "mcp-startups.log");
const probe = path.join(root, "mcp-probe.mjs");
// A real local MCP transport proves that no-MCP children do not even start servers.
// The SDK-registered stand-ins below separately exercise the capability snapshot.
fs.writeFileSync(probe, `
import fs from "node:fs";
import readline from "node:readline";
fs.appendFileSync(process.argv[2], "started\\n");
for await (const line of readline.createInterface({ input: process.stdin })) {
  const request = JSON.parse(line);
  if (request.id === undefined) continue;
  const result = request.method === "initialize"
    ? { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: "offline-probe", version: "1" } }
    : request.method === "tools/list" ? { tools: [] } : {};
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n");
}
`);
fs.writeFileSync(path.join(agentDir, "mcp.json"), JSON.stringify({
  mcpServers: { "startup-probe": { command: process.execPath, args: [probe, startupLog], exposure: "codemode" } },
}));
const startupCount = () => fs.existsSync(startupLog) ? fs.readFileSync(startupLog, "utf8").trim().split("\n").length : 0;
fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({
  defaultTools: ["+codemode"], codemode: { mode: "only" },
  retry: { enabled: false }, compaction: { enabled: false },
}));
// Never read real auth/settings, contact a model, or permit accidental HTTP fallback.
globalThis.fetch = () => { throw new Error("Network access is forbidden in subagent tests"); };
const hostPath = path.join(nodeModules, "@earendil-works/pi-coding-agent/dist");
const host = await import(pathToFileURL(path.join(hostPath, "index.js")));
const ai = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-ai/dist/index.js")));
host.initTheme("dark", false);
const originalCwd = process.cwd();
process.chdir(cwd);
let parent;
let service;
after(async () => {
  try {
    if (parent) {
      await parent.extensionRunner.emit({ type: "session_shutdown", reason: "exit" });
      parent.dispose();
    }
  } finally {
    process.chdir(originalCwd);
    delete globalThis.__envySubagentFixture;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

const calls = [];
const results = [];
const sessions = [];
let keepCalling = false;
let lateTool = false;
const fixture = globalThis.__envySubagentFixture = {
  sessions, results,
  tools: () => [
    "mcp__deepseek_search__search", "mcp__deepseek_search__fetch",
    "mcp__mcp_nixos__nix", "mcp__mcp_nixos__nix_versions",
    "mcp__unrelated__write", ...(lateTool ? ["mcp__deepseek_search__late"] : []),
  ],
  stream(model, context) {
    const stream = ai.createAssistantMessageEventStream();
    const prompt = ai.getCurrentSystemPrompt(context.messages);
    const role = /<active_agent name="([^"]+)"/.exec(prompt)?.[1];
    const priorResult = context.messages.findLast(message => message.role === "toolResult" && message.toolName === "codemode");
    if (role) calls.push({ model: model.id, provider: model.provider, role, tools: ai.getCurrentTools(context.messages) });
    const script = `
      const names = ALL_TOOLS.map(t => t.name).sort();
      const evidence = await tools.read({ path: "evidence.txt" });
      const mcp = [];
      for (const name of names.filter(n => n.startsWith("mcp__"))) {
        mcp.push({ name, result: await tools[name]({}) });
      }
      return JSON.stringify({ names, evidence, mcp,
        forbidden: ["bash", "edit", "write", "mcp__unrelated__write"].filter(n => n in tools) });
    `;
    // Completion notices can wake the parent. Only children should execute fixture tools.
    const useTool = Boolean(role) && (keepCalling || !priorResult);
    const message = {
      role: "assistant", api: model.api, provider: model.provider, model: model.id,
      timestamp: Date.now(), stopReason: useTool ? "toolUse" : "stop",
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      content: useTool
        ? [{ type: "toolCall", id: "offline-" + calls.length, name: "codemode", arguments: { code: script } }]
        : [{ type: "text", text: "Offline verification complete" }],
    };
    stream.push({ type: "start", partial: message });
    if (useTool) {
      stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
      stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: message.content[0], partial: message });
    } else {
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
    }
    stream.push({ type: "done", reason: message.stopReason, message });
    stream.end();
    return stream;
  },
};
// Discovered in the parent AND children: SDK registration is real; no MCP transport or network is needed.
fs.writeFileSync(path.join(agentDir, "extensions/offline-fixture.ts"), `
export default function(pi) {
  const fixture = globalThis.__envySubagentFixture;
  pi.registerProvider("envy-offline", {
    baseUrl: "http://invalid.test", apiKey: "offline-test-only", api: "envy-offline-api",
    streamSimple: fixture.stream,
    models: ["inherited", "override"].map(id => ({
      id, name: id, reasoning: false, input: ["text"], contextWindow: 200000, maxTokens: 4096,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    })),
  });
  for (const name of fixture.tools()) pi.registerTool({
    name, label: name, description: "Offline SDK registered MCP stand-in", exposure: "codemode",
    parameters: { type: "object", properties: {} },
    annotations: { readOnlyHint: !name.endsWith("__write") },
    execute: async () => ({ content: [{ type: "text", text: "offline:" + name }], details: {} }),
  });
  pi.on("session_start", (_event, ctx) => fixture.sessions.push({
    id: ctx.sessionManager.getSessionId(), tools: pi.getAllTools().map(t => t.name), model: ctx.model?.id,
  }));
  pi.on("tool_result", (event, ctx) => {
    if (event.toolName === "codemode") fixture.results.push({ id: ctx.sessionManager.getSessionId(), ...event });
  });
}
`);

test("real host discovers roles and starts capability-limited child sessions", { timeout: 60000 }, async t => {
  const loader = new host.DefaultResourceLoader({
    cwd, agentDir,
    extensionFactories: [{ name: "codemode", builtin: true, replaceable: true, factory: host.createCodemodeExtension() }],
  });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  assert.ok(loader.getExtensions().extensions.some(extension => extension.path.includes("envy-subagents")));
  const runtime = await host.ModelRuntime.create({
    authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json"),
  });
  ({ session: parent } = await host.createAgentSession({
    cwd, agentDir, resourceLoader: loader, modelRuntime: runtime,
    sessionManager: host.SessionManager.create(cwd, path.join(root, "sessions")),
  }));
  await parent.bindExtensions({});
  const model = new host.ModelRegistry(parent.modelRuntime).find("envy-offline", "inherited");
  assert.ok(model);
  await parent.setModel(model);
  // Session start captured the initial model; rebinding supplies the selected model to the service.
  await parent.bindExtensions({});
  service = globalThis[Symbol.for("@gotgenes/pi-subagents:service")];
  assert.ok(service, "The discovered extension must publish its actual service");
  const tool = parent.getAllTools().find(tool => tool.name === "subagent");
  for (const role of ["Explore", "Plan", "Researcher", "NixReviewer", "Reviewer"]) {
    assert.ok(tool.description.includes(role), role + " must appear in the real registry-backed tool");
  }

  async function spawn(role, options = {}) {
    const beforeStarts = startupCount();
    const beforeSessions = sessions.length;
    const beforeResults = results.length;
    const beforeCalls = calls.length;
    const id = service.spawn(role, "Verify offline child capabilities", { foreground: true, ...options });
    await service.waitForAll();
    const record = service.getRecord(id);
    if (keepCalling) {
      // Pi aborts the active SDK run when the extension enforces its hard ceiling.
      assert.ok(["completed", "error"].includes(record.status), JSON.stringify(record));
      if (record.status === "error") assert.match(record.error, /aborted/i);
    } else {
      assert.equal(record.status, "completed", JSON.stringify(record));
    }
    const child = sessions.slice(beforeSessions).find(session => session.id !== parent.sessionManager.getSessionId());
    assert.ok(child, "A real child must bind extensions");
    const result = results.slice(beforeResults).find(result => result.id === child.id);
    assert.ok(result, "The child must actually execute built-in codemode");
    assert.ok(!result.isError, JSON.stringify(result));
    const output = result.content.filter(block => block.type === "text").map(block => block.text).join("\n");
    assert.match(output, /Script completed/);
    const line = output.split("\n").find(line => line.startsWith('{"names":'));
    assert.ok(line, output);
    const snapshot = JSON.parse(line);
    assert.equal(snapshot.evidence, "offline child evidence\n");
    assert.equal(startupCount() - beforeStarts, ["Researcher", "NixReviewer"].includes(role) ? 1 : 0,
      role + " must load MCP only when its original capability list requires it");
    assert.deepEqual(snapshot.forbidden, []);
    for (const name of ["read", "grep", "find", "ls", "codemode"]) assert.ok(child.tools.includes(name), name);
    for (const name of ["bash", "edit", "write", "subagent", "get_subagent_result", "steer_subagent", "mcp__unrelated__write"]) {
      assert.ok(!child.tools.includes(name), role + " must not register " + name);
    }
    assert.ok(calls.slice(beforeCalls).every(call => call.role === role), JSON.stringify(calls.slice(beforeCalls).map(({ role, model }) => ({ role, model }))));
    assert.ok(calls.slice(beforeCalls).every(call => call.model === (options.model ? "override" : "inherited")));
    assert.equal(record.turnBudget.maxTurns, options.maxTurns ?? ({ Researcher: 12, NixReviewer: 12, Reviewer: 10 }[role]));
    return { child, snapshot, record, requestCount: calls.length - beforeCalls };
  }

  for (const role of ["Explore", "Plan", "Researcher", "NixReviewer", "Reviewer"]) {
    await t.test(role + " inherits the current model and exposes only its capabilities", async () => {
      const { snapshot } = await spawn(role);
      const expected = role === "Researcher" ? ["mcp__deepseek_search__fetch", "mcp__deepseek_search__search"]
        : role === "NixReviewer" ? ["mcp__mcp_nixos__nix", "mcp__mcp_nixos__nix_versions"] : [];
      assert.deepEqual(snapshot.names.filter(name => name.startsWith("mcp__")), expected);
      assert.deepEqual(snapshot.mcp.map(call => call.result), expected.map(name => "offline:" + name));
    });
  }
  await t.test("caller overrides model and the turn ceiling is enforced", async () => {
    keepCalling = true;
    try {
      const { record, requestCount } = await spawn("Reviewer", { model: "envy-offline/override", maxTurns: 3 });
      assert.equal(record.turnBudget.used, 3);
      assert.equal(record.turnBudget.phase, "exhausted");
      assert.equal(requestCount, 3, "No grace turn may contact the provider after the ceiling");
    } finally { keepCalling = false; }
  });
  await t.test("MCP wildcard uses the parent's registration snapshot", async () => {
    // The child fixture now registers a matching extra tool. Since the parent never registered it,
    // wildcard expansion must exclude it from this child, even though its extension offers it.
    lateTool = true;
    const { child, snapshot } = await spawn("Researcher");
    assert.ok(!child.tools.includes("mcp__deepseek_search__late"));
    assert.ok(!snapshot.names.includes("mcp__deepseek_search__late"));
  });
});
