import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { stripVTControlCharacters } from "node:util";
import { after, test } from "node:test";

const [source, nodeModules] = process.argv.slice(2);
assert.ok(source && nodeModules, "Usage: node pi-powerline.mjs <package> <Pi node_modules>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-powerline-"));
const home = path.join(root, "home");
const agentDir = path.join(home, ".pi", "agent");
const cwd = path.join(root, "project-中文");
for (const directory of [agentDir, cwd, path.join(root, "tmp"), path.join(agentDir, "extensions")]) fs.mkdirSync(directory, { recursive: true });
fs.symlinkSync(source, path.join(agentDir, "extensions", "envy-powerline"));
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: home, TMPDIR: path.join(root, "tmp"), PATH: "/usr/bin:/bin", PI_CODING_AGENT_DIR: agentDir, PI_NERD_FONTS: "0" });
fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({ compaction: { enabled: false }, retry: { enabled: false } }));
globalThis.fetch = () => { throw new Error("Network forbidden in footer regressions"); };
const host = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/index.js")));
const ai = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-ai/dist/index.js")));
const tui = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-tui/dist/index.js")));
const themes = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/modes/interactive/theme/theme.js")));
themes.initTheme("dark", false);
const fixtures = [];
const usage = { input: 10000, output: 2000, cacheRead: 3000, cacheWrite: 1000, totalTokens: 16000,
  cost: { input: .1, output: .1, cacheRead: .1, cacheWrite: .021, total: .321 } };

async function fixture({ interactive = true, manager = host.SessionManager.create(cwd, path.join(root, "sessions")) } = {}) {
  const captures = [];
  const errors = [];
  let component;
  let disposed = 0;
  const statuses = new Map([["queue", "Queue 中文"], ["context", "Context ready"], ["diff", "Diff ready"], ["mcp", "MCP 1/2"]]);
  const ui = {
    get theme() { return themes.theme; },
    setFooter(factory) {
      component?.dispose?.();
      component = factory?.({ requestRender() {} }, themes.theme, {
        getGitBranch: () => "功能/中文", getExtensionStatuses: () => statuses,
        onBranchChange: () => () => { disposed++; },
      });
      captures.push(factory);
    },
    setEditorComponent() { throw new Error("Footer replaced the queue editor"); },
    setHeader() { throw new Error("Footer replaced the header"); },
    setStatus(key, value) { if (value) statuses.set(key, value); else statuses.delete(key); },
    notify() {},
  };
  const loader = new host.DefaultResourceLoader({ cwd, agentDir,
    extensionFactories: [{ name: "offline-footer-model", factory: pi => pi.registerProvider("envy-footer", {
      baseUrl: "http://invalid.test", apiKey: "offline-only", api: "envy-footer-api",
      streamSimple: model => {
        const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
          stopReason: "stop", usage, content: [{ type: "text", text: "Offline footer reply" }] };
        const stream = ai.createAssistantMessageEventStream();
        stream.push({ type: "start", partial: message });
        stream.push({ type: "done", reason: "stop", message });
        stream.end(); return stream;
      },
      models: [{ id: "offline-中文", name: "Offline model", reasoning: true, input: ["text"], contextWindow: 200000,
        maxTokens: 4096, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    }) }],
  });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
  const { session } = await host.createAgentSession({ cwd, agentDir, resourceLoader: loader, modelRuntime: runtime, sessionManager: manager });
  await session.setModel(new host.ModelRegistry(runtime).find("envy-footer", "offline-中文"));
  await session.bindExtensions({ ...(interactive ? { mode: "tui", uiContext: ui } : { mode: "print" }), onError: error => errors.push(error) });
  const handle = { session, loader, captures, errors, ui, statuses,
    render: width => component.render(width), get disposed() { return disposed; },
    async close() {
      if (handle.closed) return;
      await session.extensionRunner.emit({ type: "session_shutdown", reason: "exit" });
      session.dispose(); handle.closed = true;
    },
  };
  fixtures.push(handle); return handle;
}
after(async () => {
  for (const fixture of fixtures) await fixture.close();
  fs.rmSync(root, { recursive: true, force: true });
});
const plain = lines => stripVTControlCharacters(lines.join("\n"));

test("manifest discovers only footer and never replaces header or editor", async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(source, "package.json"), "utf8"));
  assert.deepEqual(manifest.pi.extensions, ["./envy-footer.ts"]);
  const h = await fixture();
  assert.equal(h.captures.length, 1);
  assert.deepEqual(h.errors, []);
  assert.equal(h.loader.getExtensions().extensions.flatMap(extension => [...extension.tools.keys()]).length, 0);
});

test("real SDK footer shows model, branch, context, cost and extension statuses", async () => {
  const h = await fixture();
  await h.session.prompt("Run the offline footer check.");
  const text = plain(h.render(250));
  for (const marker of ["offline-中文", "功能/中文", "/200k", "$0.321", "Queue 中文", "Context ready", "Diff ready", "MCP 1/2"]) assert.ok(text.includes(marker), text);
  assert.ok(!text.includes("(auto)"), "Global compaction preference was ignored");
  assert.deepEqual(h.errors, []);
});

test("narrow widths and Chinese statuses stay inside terminal columns", async () => {
  const h = await fixture();
  h.statuses.set("long", "中文状态 with tabs\tand\nnewlines ".repeat(20));
  for (const width of [1, 2, 5, 12, 20, 40, 80, 200]) {
    for (const line of h.render(width)) {
      assert.ok(tui.visibleWidth(line) <= width, "Overflow at width " + width + ": " + line);
      assert.ok(!stripVTControlCharacters(line).includes("\n"));
    }
  }
});

test("theme changes recolor an existing footer and shutdown disposes it", async () => {
  const h = await fixture();
  const dark = h.render(200);
  themes.initTheme("light", false);
  const light = h.render(200);
  assert.notDeepEqual(light, dark);
  assert.equal(plain(light), plain(dark));
  themes.initTheme("dark", false);
  await h.close();
  assert.equal(h.disposed, 1);
  assert.equal(h.captures.at(-1), undefined);
});

test("review-model usage is counted once alongside assistant cost", async () => {
  const h = await fixture();
  await h.session.prompt("Complete one turn.");
  h.session.sessionManager.appendCustomEntry("envy-auto-assigned-review", { usage: { ...usage, cost: { ...usage.cost, total: .05 } } });
  assert.ok(plain(h.render(250)).includes("$0.371"));
});

test("a disk session retains footer usage after shutdown and restore", async () => {
  const first = await fixture();
  await first.session.prompt("Persist a real offline reply.");
  const filename = first.session.sessionManager.getSessionFile();
  assert.ok(filename && fs.existsSync(filename));
  await first.close();
  const restored = await fixture({ manager: host.SessionManager.open(filename) });
  assert.ok(plain(restored.render(250)).includes("$0.321"));
  assert.deepEqual(restored.errors, []);
});

test("print mode never installs a terminal footer", async () => {
  const h = await fixture({ interactive: false });
  await h.session.prompt("Print a plain offline reply.");
  assert.equal(h.captures.length, 0);
  assert.deepEqual(h.errors, []);
});
