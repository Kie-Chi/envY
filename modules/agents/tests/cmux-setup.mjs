import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { after, test } from "node:test";

const setup = path.resolve(process.argv[2]);
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-cmux-setup-"));
after(() => fs.rmSync(root, { recursive: true, force: true }));
const cli = path.join(root, "cmux");
const log = path.join(root, "calls.jsonl");
const destination = path.join(root, "config/pi-session.mjs");
fs.writeFileSync(cli, "#!" + process.execPath + '\n' + [
  'const fs = require("node:fs");',
  'const args = process.argv.slice(2);',
  'fs.appendFileSync(process.env.CMUX_TEST_LOG, JSON.stringify(args) + "\\n");',
  'if (process.env.CMUX_TEST_FAIL) process.exit(1);',
  'if (args.includes("install") && !fs.existsSync(require("node:path").join(require("node:os").homedir(), ".codex"))) process.exit(2);',
  'if (args.includes("extension-source")) process.stdout.write(process.env.CMUX_TEST_SOURCE);',
  'else process.stdout.write("Codex hooks installed\\n");',
].join('\n'), { mode: 0o700 });
const source = '// cmux-pi-session-extension-marker v4\nexport default function bridge(pi: unknown) {}\n';
function run(agents, extra = {}) {
  return spawnSync(process.execPath, ["--disable-warning=ExperimentalWarning", setup, cli, destination, ...agents], {
    encoding: "utf8", timeout: 10000,
    env: { HOME: root, PATH: path.dirname(process.execPath), CMUX_TEST_LOG: log, CMUX_TEST_SOURCE: source, ...extra },
  });
}
function calls() { return fs.readFileSync(log, "utf8").trim().split('\n').map(line => JSON.parse(line).slice(2)); }

test("selected native hooks are generated without socket discovery or unrelated agents", () => {
  const result = run(["pi", "codex"]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(calls(), [["hooks", "pi", "extension-source"], ["hooks", "codex", "install", "--yes"]]);
  assert.ok(fs.readFileSync(destination, "utf8").includes("export default function bridge"));
  assert.ok(!fs.readFileSync(destination, "utf8").includes(": unknown"));
  assert.equal(fs.statSync(destination).mode & 0o777, 0o600);
});

test("an unchanged Pi bridge is not rewritten", () => {
  const before = fs.statSync(destination);
  const result = run(["pi"]);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(fs.statSync(destination).ino, before.ino);
  assert.equal(fs.statSync(destination).mtimeMs, before.mtimeMs);
});

test("invalid native output and native command failures retain the working bridge", () => {
  const before = fs.readFileSync(destination, "utf8");
  for (const extra of [{ CMUX_TEST_SOURCE: "invalid" }, { CMUX_TEST_FAIL: "1" }, {
    CMUX_TEST_SOURCE: "// cmux-pi-session-extension-marker v4\nthis is invalid typescript {",
  }]) {
    const result = run(["pi"], extra);
    assert.notEqual(result.status, 0);
    assert.equal(fs.readFileSync(destination, "utf8"), before);
  }
  assert.ok(!fs.readdirSync(path.dirname(destination)).some(name => name.endsWith(".tmp")));
});

test("Codex-only setup leaves Pi untouched", () => {
  fs.unlinkSync(log);
  const before = fs.statSync(destination).ino;
  const result = run(["codex"]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(calls(), [["hooks", "codex", "install", "--yes"]]);
  assert.equal(fs.statSync(destination).ino, before);
});
