import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { after, test } from "node:test";

const [probeInput, modulesInput] = process.argv.slice(2);
const probe = path.resolve(probeInput);
const nodeModules = path.resolve(modulesInput);
assert.ok(probe && nodeModules, "Usage: node pi-doctor.mjs <doctor.mjs> <Pi node_modules>");
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-doctor-test-")));
after(() => fs.rmSync(root, { recursive: true, force: true }));
const source = fs.readFileSync(probe, "utf8");
const gate = 'if (!resolved.startsWith("/nix/store/")) throw new Error("Invalid source");';
assert.equal(source.split(gate).length, 2, "Fixture harness must adapt exactly one store gate");
// A test-only copy accepts this one controlled fixture root. Production has no bypass.
const fixtureProbe = path.join(root, "doctor-fixture.mjs");
fs.writeFileSync(fixtureProbe, source.replace(gate,
  `if (!resolved.startsWith("/nix/store/") && !resolved.startsWith(${JSON.stringify(root + path.sep)})) throw new Error("Invalid source");`));
const host = path.join(nodeModules, "@earendil-works/pi-coding-agent");
const hostVersion = JSON.parse(fs.readFileSync(path.join(host, "package.json"), "utf8")).version;
const packageRoot = path.join(root, "extension");
fs.mkdirSync(path.join(packageRoot, "src"), { recursive: true });
fs.writeFileSync(path.join(packageRoot, "package.json"), JSON.stringify({ pi: { extensions: ["index.ts"] } }));
fs.writeFileSync(path.join(packageRoot, "src/pi-version.ts"), 'export const MIN_PI_VERSION = "1.0.0";\n');
fs.writeFileSync(path.join(packageRoot, "index.ts"), `
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import net from "node:net";
export default async function(pi) {
  console.log("secret-extension-log");
  process.stderr.write("secret-extension-stderr");
  if (process.env.API_KEY || process.env.OPENAI_API_KEY) throw new Error("secret inherited credential");
  if (!process.env.PI_CODING_AGENT_DIR.startsWith(os.homedir())) throw new Error("unsafe agent directory");
  fs.writeFileSync(path.join(os.homedir(), "temporary-marker"), "isolated");
  for (const operation of [() => fetch("https://invalid.example"), () => spawnSync("sh", ["-c", "false"]), () => net.connect(80, "invalid.example")]) {
    let blocked = false;
    try { await operation(); } catch { blocked = true; }
    if (!blocked) throw new Error("operation was not blocked");
  }
  pi.registerCommand("doctor:fixture", { description: "fixture", handler() {} });
  pi.registerTool({ name: "doctor_tool", label: "Fixture", description: "Fixture", parameters: { type: "object", properties: {} }, execute() {} });
  setInterval(() => {}, 1000);
}
`);
const malicious = path.join(root, "throw.ts");
fs.writeFileSync(malicious, 'export default function () { console.log("secret-before-error"); throw new Error("secret-extension-error"); }');
const unrelated = path.join(root, "home/.pi/agent/extensions");
fs.mkdirSync(unrelated, { recursive: true });
fs.writeFileSync(path.join(unrelated, "unmanaged.ts"), 'throw new Error("unmanaged extension loaded");');
fs.writeFileSync(path.join(root, "home/.pi/agent/auth.json"), "secret-auth-sentinel");
fs.writeFileSync(path.join(root, "home/.pi/agent/settings.json"), "secret-settings-sentinel");

function run(entries, script = fixtureProbe) {
  const input = { schemaVersion: 1, pi: { nodeModules }, extensions: entries };
  const completed = spawnSync(process.execPath, [script], {
    input: JSON.stringify(input), encoding: "utf8", timeout: 20000,
    cwd: root,
    env: { HOME: path.join(root, "home"), TMPDIR: root, API_KEY: "secret-key", OPENAI_API_KEY: "secret-openai" },
  });
  assert.equal(completed.error, undefined);
  assert.equal(completed.stderr, "");
  assert.ok(!completed.stdout.includes("secret"), "Logs and raw errors must not escape");
  return { code: completed.status, data: JSON.parse(completed.stdout) };
}

test("real Pi discovers a managed extension in isolation and blocks network/process side effects", () => {
  const before = fs.readFileSync(path.join(root, "home/.pi/agent/auth.json"), "utf8");
  const result = run([{ name: "envy-fixture", source: packageRoot }]);
  assert.equal(result.code, 0);
  assert.equal(result.data.status, "ok");
  assert.equal(result.data.hostVersion, hostVersion);
  assert.deepEqual(result.data.extensions[0].tools, ["doctor_tool"]);
  assert.deepEqual(result.data.extensions[0].commands, ["doctor:fixture"]);
  assert.deepEqual(result.data.extensions[0].guard, {
    status: "ok", minimum: "1.0.0", declaration: "src/pi-version.ts:MIN_PI_VERSION",
  });
  assert.equal(fs.readFileSync(path.join(root, "home/.pi/agent/auth.json"), "utf8"), before);
  assert.equal(fs.readFileSync(path.join(root, "home/.pi/agent/settings.json"), "utf8"), "secret-settings-sentinel");
  assert.ok(!fs.existsSync(path.join(root, "home/temporary-marker")));
  assert.equal(fs.readdirSync(root).filter(name => name.startsWith("envy-pi-doctor-")).length, 0);
});

test("production store validation loads the actual host example without user extensions", () => {
  const result = run([{ name: "envy-hello.ts", source: path.join(host, "examples/extensions/hello.ts") }], probe);
  assert.equal(result.code, 0);
  assert.deepEqual(result.data.extensions[0].tools, ["hello"]);
  assert.equal(result.data.extensions[0].guard.status, "unknown");
});

test("factory errors expose only safe status and counts", () => {
  const result = run([{ name: "envy-throw.ts", source: malicious }]);
  assert.equal(result.code, 1);
  assert.equal(result.data.status, "error");
  assert.equal(result.data.extensions[0].errorCount, 1);
  assert.equal(result.data.extensions[0].status, "error");
});

test("cron factory is skipped without invoking side effects", () => {
  const result = run([{ name: "envy-cron", source: malicious }]);
  assert.equal(result.code, 0);
  assert.equal(result.data.extensions[0].status, "skipped");
});

test("a static minimum newer than the host blocks its factory", () => {
  fs.writeFileSync(path.join(packageRoot, "src/pi-version.ts"), 'export const MIN_PI_VERSION = "999.0.0";\n');
  try {
    const result = run([{ name: "envy-fixture", source: packageRoot }]);
    assert.equal(result.code, 1);
    assert.equal(result.data.extensions[0].status, "unsupported");
    assert.equal(result.data.extensions[0].loadedCount, undefined);
    assert.equal(result.data.extensions[0].guard.status, "error");
  } finally {
    fs.writeFileSync(path.join(packageRoot, "src/pi-version.ts"), 'export const MIN_PI_VERSION = "1.0.0";\n');
  }
});

test("commented minimum declarations never establish compatibility", () => {
  fs.writeFileSync(path.join(packageRoot, "src/pi-version.ts"), '/*\nexport const MIN_PI_VERSION = "999.0.0";\n*/\n');
  try {
    const result = run([{ name: "envy-fixture", source: packageRoot }]);
    assert.equal(result.code, 0);
    assert.equal(result.data.extensions[0].guard.status, "unknown");
  } finally {
    fs.writeFileSync(path.join(packageRoot, "src/pi-version.ts"), 'export const MIN_PI_VERSION = "1.0.0";\n');
  }
});

test("production rejects temporary/unmanaged sources", () => {
  assert.equal(run([{ name: "envy-fixture", source: packageRoot }], probe).code, 1);
  assert.equal(run([{ name: "../escape", source: packageRoot }]).code, 1);
});
