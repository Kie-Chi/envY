import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

const [diffPackage, nodeModules] = process.argv.slice(2);
assert.ok(diffPackage && nodeModules, "Usage: node pi-diff.mjs <diffPackage> <Pi node_modules>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-diff-"));
const agentDir = path.join(root, "agent");
const cwd = path.join(root, "project");
const home = path.join(root, "home");
for (const directory of [cwd, home, path.join(root, "tmp"), path.join(agentDir, "extensions")]) fs.mkdirSync(directory, { recursive: true });
const testPath = process.env.PATH;
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: home, TMPDIR: path.join(root, "tmp"), PATH: testPath, PI_CODING_AGENT_DIR: agentDir,
  GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" });
fs.symlinkSync(fs.realpathSync(diffPackage), path.join(agentDir, "extensions/envy-diff"));
globalThis.fetch = () => { throw new Error("Network access is forbidden in diff tests"); };
const git = (...args) => execFileSync("git", args, { cwd });
git("init", "-q");
const sample = path.join(cwd, "sample.txt");
fs.writeFileSync(sample, "before-envy-diff\n");
git("add", "sample.txt");
fs.writeFileSync(sample, "after-envy-diff\n");
const index = fs.readFileSync(path.join(cwd, ".git/index"));
const originalCwd = process.cwd();
process.chdir(cwd);
const hostPath = path.join(nodeModules, "@earendil-works/pi-coding-agent/dist");
const host = await import(pathToFileURL(path.join(hostPath, "index.js")));
const themes = await import(pathToFileURL(path.join(hostPath, "modes/interactive/theme/theme.js")));
host.initTheme("dark", false);
const loader = new host.DefaultResourceLoader({ cwd, agentDir });
await loader.reload();
assert.deepEqual(loader.getExtensions().errors, []);
const extension = loader.getExtensions().extensions.find(e => e.path.includes("envy-diff"));
assert.ok(extension, "real SDK must discover the package manifest");
const command = extension.commands.get("diff");
assert.ok(command, "real SDK must register /diff");
const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
const { session } = await host.createAgentSession({ cwd, agentDir, resourceLoader: loader, modelRuntime: runtime,
  sessionManager: host.SessionManager.inMemory(cwd) });
await session.bindExtensions({});
after(async () => {
  try {
    await session.extensionRunner.emit({ type: "session_shutdown", reason: "exit" });
    session.dispose();
  } finally { process.chdir(originalCwd); fs.rmSync(root, { recursive: true, force: true }); }
});

test("real SDK command renders actual Git index differences with the real component", async () => {
  const notices = [];
  const rendered = [];
  const base = session.extensionRunner.createCommandContext();
  const ctx = Object.create(base);
  Object.defineProperties(ctx, {
    mode: { value: "tui" }, hasUI: { value: true },
    ui: { value: {
      notify: message => notices.push(message),
      custom: async factory => {
        let closed = false;
        const component = factory({ terminal: { rows: 50, columns: 120 }, requestRender() {} }, themes.theme, {}, () => { closed = true; });
        rendered.push(component.render(120).join("\n"));
        component.handleInput("v");
        rendered.push(component.render(120).join("\n"));
        component.handleInput("q");
        assert.equal(closed, true);
      },
    } },
  });
  await command.handler("", ctx);
  assert.deepEqual(notices, []);
  assert.equal(rendered.length, 2);
  for (const text of rendered) {
    assert.match(text, /sample\.txt/);
    assert.match(text, /before-envy-diff/);
    assert.match(text, /after-envy-diff/);
  }
  assert.notEqual(rendered[0], rendered[1], "v must change the rendered layout");
  assert.equal(fs.readFileSync(sample, "utf8"), "after-envy-diff\n");
  assert.deepEqual(fs.readFileSync(path.join(cwd, ".git/index")), index);
});
